import asyncio
import logging
import tarfile
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

        has_wow64 = False
        processed_artifacts = []

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            for artifact in artifacts_response.artifacts:
                if artifact.expired:
                    continue

                # Filter artifacts: skip gcc builds
                if "-gcc" in artifact.name.lower():
                    continue

                if "release" not in artifact.name.lower():
                    continue

                is_wow64_artifact = "wow64" in artifact.name.lower()

                # Download artifact zip
                logger.info(f"Downloading artifact {artifact.name} from run {run.id}")
                zip_path = temp_path / f"{artifact.name}.zip"
                self.github_client.download_artifact(zip_path, self.owner, self.repo, artifact.id)

                # Extract zip
                extract_dir = temp_path / artifact.name
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(extract_dir)

                # Find and extract tar.gz inside
                tar_files = list(extract_dir.glob("*.tar.gz"))
                if not tar_files:
                    logger.warning(f"No tar.gz found in artifact {artifact.name}")
                    continue

                tar_path = tar_files[0]
                tar_extract_dir = extract_dir / "extracted"
                with tarfile.open(tar_path, "r:gz") as tar_ref:
                    tar_ref.extractall(tar_extract_dir)

                # Inspect extracted files
                for file_path in tar_extract_dir.rglob("*"):
                    if not file_path.is_file():
                        continue

                    # Filter files based on artifact type
                    if is_wow64_artifact:
                        if file_path.suffix != ".dll":
                            continue
                        has_wow64 = True
                    else:
                        if file_path.suffix not in [".dll", ".so"]:
                            continue

                    # Create DB object
                    db_artifact = BuiltinArtifact(
                        artifact_id=artifact.id,
                        build_id=run.id,
                        name=file_path.name,
                        is_wow64=is_wow64_artifact
                    )

                    # Upload to S3
                    key = artifact_manager._get_s3_key(db_artifact)
                    artifact_manager.s3_client.upload_file(str(file_path), artifact_manager.bucket_name, key)
                    processed_artifacts.append(db_artifact)

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

        asset = release.assets[0]
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

            # Check folders and upload
            i386_windows = extract_dir / "i386-windows"
            x86_64_windows = extract_dir / "x86_64-windows"
            x86_64_unix = extract_dir / "x86_64-unix"

            if i386_windows.exists():
                has_wow64 = True
                self._upload_release_files(release, i386_windows, True, processed_artifacts, artifact_manager)

            if x86_64_windows.exists():
                self._upload_release_files(release, x86_64_windows, False, processed_artifacts, artifact_manager)

            if x86_64_unix.exists():
                self._upload_release_files(release, x86_64_unix, False, processed_artifacts, artifact_manager)

        self._save_release_build(release, processed_artifacts, has_wow64, session)

    def _upload_release_files(self, release: GitHubRelease, directory: Path, is_wow64: bool, processed_list: list, artifact_manager: DXMTArtifactManager):
        for file_path in directory.rglob("*"):
            if not file_path.is_file():
                continue

            db_artifact = ReleaseArtifact(
                build_tag=release.tag_name,
                name=file_path.name,
                is_wow64=is_wow64
            )

            key = artifact_manager._get_s3_key(db_artifact)
            artifact_manager.s3_client.upload_file(str(file_path), artifact_manager.bucket_name, key)
            processed_list.append(db_artifact)

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
