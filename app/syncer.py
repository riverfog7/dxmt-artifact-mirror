import asyncio
import logging
import tarfile
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlmodel import Session, select, col

from .artifact_manager import DXMTArtifactManager
from .github import GitHubAPIClient
from .models.builds import BuiltinBuild, ReleaseBuild, BuiltinArtifact, ReleaseArtifact
from .models.github import GitHubActionRun, GitHubRelease
from .utils import get_endpoint_url

logger = logging.getLogger(__name__)

class ArtifactSyncer:
    def __init__(self, github_client: GitHubAPIClient, engine, bucket_name: str):
        self.github_client = github_client
        self.engine = engine
        self.bucket_name = bucket_name
        self.owner = "3Shain"
        self.repo = "dxmt"

    async def sync_loop(self):
        while True:
            try:
                # Run the blocking sync cycle in a separate thread to avoid blocking the event loop
                await asyncio.to_thread(self._run_sync_cycle)
            except Exception as e:
                logger.error(f"Error in sync cycle: {e}", exc_info=True)

            await asyncio.sleep(60)

    def _run_sync_cycle(self):
        logger.info("Starting sync cycle...")
        with Session(self.engine) as session:
            artifact_manager = DXMTArtifactManager(session, self.bucket_name, endpoint_url=get_endpoint_url())
            self.sync_builtin_builds(session, artifact_manager)
            self.sync_releases(session, artifact_manager)
        logger.info("Sync cycle completed.")

    def sync_builtin_builds(self, session: Session, artifact_manager: DXMTArtifactManager):
        logger.info("Syncing builtin builds...")
        # Get the latest build we have in DB
        latest_build = session.exec(
            select(BuiltinBuild).order_by(col(BuiltinBuild.created_at).desc())
        ).first()

        latest_run_id = latest_build.github_run_id if latest_build else 0

        new_runs = []
        page = 1
        should_continue = True

        while should_continue:
            # Fetch runs from GitHub
            runs_response = self.github_client.get_action_runs(self.owner, self.repo, page=page, status="success")

            if not runs_response.workflow_runs:
                break

            for run in runs_response.workflow_runs:
                if run.id <= latest_run_id:
                    should_continue = False
                    break

                # if the run date is older than 4 months, stop processing further
                if run.created_at < datetime.now(timezone.utc) - timedelta(days=100):
                    should_continue = False
                    break

                # Filter out runs that are not successful or are native builds
                # status="success" filter handles status/conclusion check

                if "native" in run.path:
                    continue

                new_runs.append(run)

            if len(runs_response.workflow_runs) < 30:
                should_continue = False

            page += 1

        # Process new runs (oldest first to maintain order if we stop)
        for run in reversed(new_runs):
            self._process_builtin_run(run, session, artifact_manager)

    def _process_builtin_run(self, run: GitHubActionRun, session: Session, artifact_manager: DXMTArtifactManager):
        logger.info(f"Processing new run: {run.id}")

        # Fetch artifacts for this run
        artifacts_response = self.github_client.get_run_artifacts(self.owner, self.repo, run.id)

        if not artifacts_response.artifacts:
            logger.info(f"Run {run.id} has no artifacts. Skipping.")
            return

        selected_artifacts = self._select_builtin_source_artifacts(run, artifacts_response.artifacts)
        if not selected_artifacts:
            logger.info(f"Run {run.id} has no compatible builtin artifacts. Skipping.")
            return

        has_wow64 = False
        processed_artifacts = []

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            for artifact in selected_artifacts:
                # Download artifact zip
                logger.info(f"Downloading artifact {artifact.name} from run {run.id}")
                zip_path = temp_path / f"{artifact.name}.zip"
                self.github_client.download_artifact(zip_path, self.owner, self.repo, artifact.id)

                # Extract zip
                extract_dir = temp_path / artifact.name
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_dir)

                combined_name = f"dxmt-{run.head_sha}"
                if artifact.name == combined_name:
                    collected_files = self._extract_combined_ci_artifact(extract_dir)
                elif artifact.name == "artifacts-release-gcc":
                    collected_files = self._extract_legacy_split_ci_artifact(extract_dir, is_wow64=False)
                elif artifact.name == "artifacts-release-wow64-gcc":
                    collected_files = self._extract_legacy_split_ci_artifact(extract_dir, is_wow64=True)
                else:
                    collected_files = self._extract_split_ci_artifact(extract_dir)

                for file_path, is_wow64 in collected_files:
                    # Create DB object
                    db_artifact = BuiltinArtifact(
                        artifact_id=artifact.id,
                        build_id=run.id,
                        name=file_path.name,
                        is_wow64=is_wow64
                    )

                    # Upload to S3
                    key = artifact_manager._get_s3_key(db_artifact)
                    artifact_manager.s3_client.upload_file(str(file_path), artifact_manager.bucket_name, key)
                    processed_artifacts.append(db_artifact)
                    if is_wow64:
                        has_wow64 = True

        if not processed_artifacts:
            logger.info(f"Run {run.id} has no relevant artifacts. Skipping.")
            return

        # Save build and artifacts to DB
        build = BuiltinBuild(
            github_run_id=run.id,
            commit_sha=run.head_sha,
            description=run.display_title,
            created_at=run.created_at,
            artifact_count=len(processed_artifacts),
            has_wow64=has_wow64
        )

        session.add(build)
        for art in processed_artifacts:
            session.add(art)
        session.commit()
        logger.info(f"Saved run {run.id} with {len(processed_artifacts)} artifacts")

    def _select_builtin_source_artifacts(self, run: GitHubActionRun, artifacts: list):
        active_artifacts = [artifact for artifact in artifacts if not artifact.expired]
        artifacts_by_name = {artifact.name: artifact for artifact in active_artifacts}

        combined_name = f"dxmt-{run.head_sha}"
        if combined_name in artifacts_by_name:
            return [artifacts_by_name[combined_name]]

        legacy_selected = []
        for name in ("artifacts-release-gcc", "artifacts-release-wow64-gcc"):
            artifact = artifacts_by_name.get(name)
            if artifact:
                legacy_selected.append(artifact)
        if legacy_selected:
            return legacy_selected

        selected = []
        for name in ("gcc-release-x86_64-windows-cross", "gcc-release-x86-windows-cross"):
            artifact = artifacts_by_name.get(name)
            if artifact:
                selected.append(artifact)
        return selected

    def _extract_combined_ci_artifact(self, extract_dir: Path) -> list[tuple[Path, bool]]:
        tar_files = sorted(extract_dir.glob("*.tar.gz"))
        if not tar_files:
            logger.warning(f"No combined tarball found in artifact directory {extract_dir}")
            return []

        tar_path = tar_files[0]
        tar_extract_dir = extract_dir / "tar_extracted"
        with tarfile.open(tar_path, "r:gz") as tar_ref:
            tar_ref.extractall(tar_extract_dir)

        runtime_root = self._find_single_top_level_dir(tar_extract_dir)
        if runtime_root is None:
            logger.warning(f"Combined artifact tarball {tar_path.name} did not extract to a single top-level directory")
            return []

        return self._collect_runtime_files(runtime_root)

    def _extract_split_ci_artifact(self, extract_dir: Path) -> list[tuple[Path, bool]]:
        return self._collect_runtime_files(extract_dir)

    def _extract_legacy_split_ci_artifact(self, extract_dir: Path, is_wow64: bool) -> list[tuple[Path, bool]]:
        tar_files = sorted(extract_dir.glob("*.tar.gz"))
        if not tar_files:
            logger.warning(f"No legacy tarball found in artifact directory {extract_dir}")
            return []

        tar_path = tar_files[0]
        tar_extract_dir = extract_dir / "tar_extracted"
        with tarfile.open(tar_path, "r:gz") as tar_ref:
            tar_ref.extractall(tar_extract_dir)

        runtime_root = self._find_legacy_runtime_root(tar_extract_dir)
        if runtime_root is None:
            logger.warning(f"Legacy artifact tarball {tar_path.name} did not extract to a build-release*/src directory")
            return []

        return self._collect_legacy_runtime_files(runtime_root, is_wow64=is_wow64)

    def _find_single_top_level_dir(self, root: Path) -> Optional[Path]:
        directories = [path for path in root.iterdir() if path.is_dir()]
        if len(directories) == 1:
            return directories[0]
        return None

    def _find_legacy_runtime_root(self, root: Path) -> Optional[Path]:
        top_level_dir = self._find_single_top_level_dir(root)
        if top_level_dir is None:
            return None

        runtime_root = top_level_dir / "src"
        if runtime_root.is_dir():
            return runtime_root
        return None

    def _collect_runtime_files(self, runtime_root: Path) -> list[tuple[Path, bool]]:
        collected = []

        i386_dir = runtime_root / "i386-windows"
        if i386_dir.is_dir():
            for file_path in i386_dir.rglob("*"):
                if file_path.is_file() and file_path.suffix == ".dll":
                    collected.append((file_path, True))

        x64_windows_dir = runtime_root / "x86_64-windows"
        if x64_windows_dir.is_dir():
            for file_path in x64_windows_dir.rglob("*"):
                if file_path.is_file() and file_path.suffix == ".dll":
                    collected.append((file_path, False))

        x64_unix_dir = runtime_root / "x86_64-unix"
        if x64_unix_dir.is_dir():
            for file_path in x64_unix_dir.rglob("*"):
                if file_path.is_file() and file_path.suffix == ".so":
                    collected.append((file_path, False))

        return collected

    def _collect_legacy_runtime_files(self, runtime_root: Path, is_wow64: bool) -> list[tuple[Path, bool]]:
        collected = []

        for file_path in runtime_root.rglob("*"):
            if not file_path.is_file():
                continue
            if file_path.suffix == ".dll":
                collected.append((file_path, is_wow64))
            elif not is_wow64 and file_path.suffix == ".so":
                collected.append((file_path, False))

        return collected

    def sync_releases(self, session: Session, artifact_manager: DXMTArtifactManager):
        logger.info("Syncing releases...")

        new_releases = []
        page = 1
        should_continue = True

        while should_continue:
            # Fetch releases
            releases = self.github_client.get_releases(self.owner, self.repo, page=page)

            if not releases:
                break

            for release in releases:
                # Check if we already have this tag
                existing = session.get(ReleaseBuild, release.tag_name)
                if existing:
                    should_continue = False
                    break # Assume ordered by date
                if not release.draft and not release.prerelease:
                    new_releases.append(release)

            if len(releases) < 30:
                should_continue = False

            page += 1

        for release in reversed(new_releases):
            self._process_release(release, session, artifact_manager)

    def _process_release(self, release: GitHubRelease, session: Session, artifact_manager: DXMTArtifactManager):
        logger.info(f"Processing new release: {release.tag_name}")

        if not release.assets:
            logger.info(f"Skipping release {release.tag_name} (no assets)")
            # We still save the build to avoid reprocessing, but with 0 artifacts
            self._save_release_build(release, [], False, session)
            return

        asset = self._select_release_asset(release)
        if asset is None:
            logger.info(f"Skipping release {release.tag_name} (no compatible tar.gz asset)")
            self._save_release_build(release, [], False, session)
            return

        has_wow64 = False
        processed_artifacts = []

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            # Download asset
            asset_path = temp_path / asset.name
            self.github_client.download_release_asset(asset_path, self.owner, self.repo, asset.id)

            # Extract tar.gz
            extract_dir = temp_path / "extracted"
            try:
                with tarfile.open(asset_path, "r:gz") as tar_ref:
                    tar_ref.extractall(extract_dir)
            except Exception as e:
                logger.error(f"Failed to extract release asset {asset.name}: {e}")
                self._save_release_build(release, [], False, session)
                return

            runtime_root = self._find_single_top_level_dir(extract_dir)
            if runtime_root is None:
                logger.warning(f"Release asset {asset.name} did not extract to a single top-level directory")
                self._save_release_build(release, [], False, session)
                return

            collected_files = self._collect_runtime_files(runtime_root)
            has_wow64 = any(is_wow64 for _, is_wow64 in collected_files)

            for file_path, is_wow64 in collected_files:
                db_artifact = ReleaseArtifact(
                    build_tag=release.tag_name,
                    name=file_path.name,
                    is_wow64=is_wow64
                )
                key = artifact_manager._get_s3_key(db_artifact)
                artifact_manager.s3_client.upload_file(str(file_path), artifact_manager.bucket_name, key)
                processed_artifacts.append(db_artifact)

        self._save_release_build(release, processed_artifacts, has_wow64, session)

    def _select_release_asset(self, release: GitHubRelease):
        tar_assets = [asset for asset in release.assets if asset.name.endswith(".tar.gz")]

        for asset in tar_assets:
            if "builtin" in asset.name:
                return asset

        combined_name = f"dxmt-{release.tag_name}.tar.gz"
        for asset in tar_assets:
            if asset.name == combined_name:
                return asset

        return None

    def _save_release_build(self, release: GitHubRelease, artifacts: list, has_wow64: bool, session: Session):
        build = ReleaseBuild(
            tag=release.tag_name,
            created_at=release.created_at,
            artifact_count=len(artifacts),
            has_wow64=has_wow64
        )

        session.add(build)
        for art in artifacts:
            session.add(art)
        session.commit()
        logger.info(f"Saved release {release.tag_name} with {len(artifacts)} artifacts")
