import asyncio
import logging
import tarfile
import tempfile
import zipfile
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from requests import HTTPError
from sqlmodel import Session, select, col

from .artifact_manager import DXMTArtifactManager
from .github import GitHubAPIClient
from .models.builds import BuiltinBuild, ReleaseBuild, BuiltinArtifact, ReleaseArtifact
from .models.github import GitHubActionRun, GitHubRelease
from .utils import get_endpoint_url

logger = logging.getLogger(__name__)

class ArtifactSyncer:
    BACKFILL_BATCH_SIZE = 5
    CI_ARTIFACT_NAMES = (
        "clang-release-x86_64-windows-cross",
        "clang-release-x86-windows-cross",
        "clang-release-arm64ec-windows-cross",
    )

    def __init__(self, github_client: GitHubAPIClient, engine, bucket_name: str):
        self.github_client = github_client
        self.engine = engine
        self.bucket_name = bucket_name
        self.owner = "3Shain"
        self.repo = "dxmt"
        self._backfill_run_ids: Optional[deque[int]] = None

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
            self.backfill_builtin_builds(session, artifact_manager)
        logger.info("Sync cycle completed.")

    def backfill_builtin_builds(self, session: Session, artifact_manager: DXMTArtifactManager):
        if self._backfill_run_ids is None:
            self._backfill_run_ids = deque(session.exec(
                select(BuiltinBuild.github_run_id).order_by(col(BuiltinBuild.created_at).desc())
            ).all())
        if not self._backfill_run_ids:
            return

        for _ in range(min(self.BACKFILL_BATCH_SIZE, len(self._backfill_run_ids))):
            run_id = self._backfill_run_ids.popleft()
            build = session.get(BuiltinBuild, run_id)
            if build is None:
                continue
            try:
                self._sync_builtin_artifacts(build, session, artifact_manager)
            except Exception:
                session.rollback()
                self._backfill_run_ids.append(run_id)
                logger.error(f"Failed to backfill CI run {run_id}; will retry", exc_info=True)

        logger.info(f"CI backfill: {len(self._backfill_run_ids)} runs remaining")

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
        build = session.get(BuiltinBuild, run.id)
        if build is None:
            build = BuiltinBuild(
                github_run_id=run.id,
                commit_sha=run.head_sha,
                description=run.display_title,
                created_at=run.created_at,
                artifact_count=0,
            )
        try:
            self._sync_builtin_artifacts(build, session, artifact_manager)
        except Exception:
            session.rollback()
            raise

    def _sync_builtin_artifacts(self, build: BuiltinBuild, session: Session, artifact_manager: DXMTArtifactManager):
        source_artifacts = []
        page = 1
        while True:
            try:
                response = self.github_client.get_run_artifacts(
                    self.owner, self.repo, build.github_run_id, per_page=100, page=page,
                )
            except HTTPError as error:
                if error.response is not None and error.response.status_code in (404, 410):
                    logger.info(f"Run {build.github_run_id} is no longer available. Skipping.")
                    return
                raise
            source_artifacts.extend(response.artifacts)
            if not response.artifacts or len(source_artifacts) >= response.total_count:
                break
            page += 1

        selected_artifacts = self._select_builtin_source_artifacts(source_artifacts)
        if not selected_artifacts:
            logger.info(f"Run {build.github_run_id} has no unexpired Clang release artifacts. Skipping.")
            return

        existing_artifacts = session.exec(
            select(BuiltinArtifact).where(BuiltinArtifact.build_id == build.github_run_id)
        ).all()
        artifacts_by_key = {(a.name, a.is_wow64, a.is_arm64ec): a for a in existing_artifacts}
        imported_source_ids = {a.artifact_id for a in existing_artifacts}
        processed_artifacts = []

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            for artifact in selected_artifacts:
                if artifact.id in imported_source_ids:
                    continue
                # Download artifact zip
                logger.info(f"Downloading artifact {artifact.name} from run {build.github_run_id}")
                zip_path = temp_path / f"{artifact.name}.zip"
                try:
                    self.github_client.download_artifact(zip_path, self.owner, self.repo, artifact.id)
                except HTTPError as error:
                    if error.response is not None and error.response.status_code in (404, 410):
                        logger.info(f"Artifact {artifact.name} is no longer available. Skipping.")
                        continue
                    raise

                # Extract zip
                extract_dir = temp_path / artifact.name
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_dir)

                is_arm64ec = artifact.name == "clang-release-arm64ec-windows-cross"
                collected_files = self._collect_runtime_files(extract_dir, arm64ec=is_arm64ec)
                if not collected_files:
                    raise ValueError(f"Artifact {artifact.name} has no runtime files")

                for file_path, is_wow64 in collected_files:
                    identity = (file_path.name, is_wow64, is_arm64ec)
                    db_artifact = artifacts_by_key.get(identity)
                    if db_artifact is None:
                        db_artifact = BuiltinArtifact(
                            artifact_id=artifact.id,
                            build_id=build.github_run_id,
                            name=file_path.name,
                            is_wow64=is_wow64,
                            is_arm64ec=is_arm64ec,
                        )
                        artifacts_by_key[identity] = db_artifact

                    # Upload to S3
                    key = artifact_manager._get_s3_key(db_artifact)
                    artifact_manager.s3_client.upload_file(str(file_path), artifact_manager.bucket_name, key)
                    processed_artifacts.append((db_artifact, artifact.id))

        if not processed_artifacts:
            return

        # Source IDs are recorded only after every upload succeeds.
        build.artifact_count = len(artifacts_by_key)
        build.has_wow64 = any(a.is_wow64 for a in artifacts_by_key.values())
        session.add(build)
        for art, source_id in processed_artifacts:
            art.artifact_id = source_id
            session.add(art)
        session.commit()
        logger.info(f"Saved run {build.github_run_id} with {build.artifact_count} artifacts")

    def _select_builtin_source_artifacts(self, artifacts: list):
        artifacts_by_name = {
            artifact.name: artifact
            for artifact in sorted(artifacts, key=lambda a: a.id)
            if not artifact.expired
        }
        return [artifacts_by_name[name] for name in self.CI_ARTIFACT_NAMES if name in artifacts_by_name]

    def _find_single_top_level_dir(self, root: Path) -> Optional[Path]:
        directories = [path for path in root.iterdir() if path.is_dir()]
        if len(directories) == 1:
            return directories[0]
        return None

    def _collect_runtime_files(self, runtime_root: Path, arm64ec: bool = False) -> list[tuple[Path, bool]]:
        collected = []
        directories = (
            (("aarch64-windows", ".dll", False), ("aarch64-unix", ".so", False))
            if arm64ec else
            (("i386-windows", ".dll", True), ("x86_64-windows", ".dll", False), ("x86_64-unix", ".so", False))
        )
        for directory, suffix, is_wow64 in directories:
            for file_path in sorted((runtime_root / directory).rglob("*")):
                if file_path.is_file() and file_path.suffix == suffix:
                    collected.append((file_path, is_wow64))
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
