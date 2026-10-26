from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
from types import SimpleNamespace
import tarfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from requests import HTTPError, Response
from sqlmodel import Session, create_engine, select

from app.artifact_manager import DXMTArtifactManager
from app.github import GitHubAPIClient
from app.models.builds import BuiltinArtifact, BuiltinBuild, ReleaseArtifact
from app.models.github import GitHubActionArtifact, GitHubActionRun
from app.syncer import ArtifactSyncer
from app.utils import init_db


def source(artifact_id, name, expired=False):
    return GitHubActionArtifact(id=artifact_id, name=name, expired=expired, digest="sha256:test")


def run(run_id=7, created_at=None):
    return GitHubActionRun(
        id=run_id, name="CI Build", head_branch="main", head_sha=f"commit-{run_id}",
        display_title="test build", status="completed", conclusion="success",
        updated_at=None, created_at=created_at or datetime.now(timezone.utc), run_started_at=None,
        artifacts_url="https://api.github.com/test/artifacts", workflow_id=1, path=".github/workflows/ci.yml",
    )


class SyncerTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        init_db(self.engine)
        self.session = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.session.close)
        self.github = Mock(spec=GitHubAPIClient)
        self.sources = [
            source(10, "clang-release-x86_64-windows-cross"),
            source(11, "clang-release-x86-windows-cross"),
            source(12, "clang-release-arm64ec-windows-cross"),
        ]
        self.archives = {
            10: {
                "x86_64-windows/d3d11.dll": b"clang-x64",
                "x86_64-windows/d3d12.dll": b"d3d12",
                "x86_64-unix/winemetal.so": b"unix-x64",
                "x86_64-windows/libignored.a": b"static library",
            },
            11: {"i386-windows/d3d11.dll": b"clang-i386"},
            12: {
                "aarch64-windows/d3d11.dll": b"clang-arm64ec",
                "aarch64-unix/winemetal.so": b"unix-arm64",
            },
        }
        self.github.get_run_artifacts.side_effect = lambda *args, **kwargs: SimpleNamespace(
            total_count=len(self.sources), artifacts=self.sources,
        )
        self.github.download_artifact.side_effect = self.download
        self.s3 = Mock()
        self.objects = {}
        self.s3.upload_file.side_effect = self.upload
        with patch("app.artifact_manager.boto3.client", return_value=self.s3):
            self.manager = DXMTArtifactManager(self.session, "test")
        self.syncer = ArtifactSyncer(self.github, self.engine, "test")

    def download(self, path, owner, repo, artifact_id):
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in self.archives[artifact_id].items():
                archive.writestr(name, data)

    def upload(self, path, bucket, key):
        self.objects[key] = Path(path).read_bytes()

    def artifacts(self, run_id=7):
        return self.session.exec(select(BuiltinArtifact).where(BuiltinArtifact.build_id == run_id)).all()

    def add_legacy_build(self, run_id=7):
        self.session.add(BuiltinBuild(
            github_run_id=run_id, commit_sha=f"commit-{run_id}", description="legacy build",
            created_at=datetime.now(timezone.utc)-timedelta(days=365-run_id),
            artifact_count=2, has_wow64=True,
        ))
        for wow64 in (False, True):
            artifact = BuiltinArtifact(artifact_id=900, build_id=run_id, name="d3d11.dll", is_wow64=wow64)
            self.session.add(artifact)
            self.objects[self.manager._get_s3_key(artifact)] = b"old-gcc"
        self.session.commit()

    def test_backfill_replaces_gcc_and_adds_arm64ec_and_d3d12(self):
        self.add_legacy_build()
        original_ids = {(a.name, a.is_wow64, a.is_arm64ec): a.id for a in self.artifacts()}
        self.syncer.backfill_builtin_builds(self.session, self.manager)
        artifacts = self.artifacts()
        self.assertEqual(len(artifacts), 6)
        self.assertEqual(self.session.get(BuiltinBuild, 7).artifact_count, 6)
        self.assertTrue(self.session.get(BuiltinBuild, 7).has_wow64)
        for artifact in artifacts:
            identity = (artifact.name, artifact.is_wow64, artifact.is_arm64ec)
            if identity in original_ids:
                self.assertEqual(artifact.id, original_ids[identity])
        self.assertEqual({a.artifact_id for a in artifacts}, {10, 11, 12})
        self.assertEqual(self.objects["dxmt-artifacts/builtin/7/d3d11.dll"], b"clang-x64")
        self.assertEqual(self.objects["dxmt-artifacts/wow64/builtin/7/d3d11.dll"], b"clang-i386")
        self.assertEqual(self.objects["dxmt-artifacts/builtin/7/d3d12.dll"], b"d3d12")
        self.assertEqual(self.objects["dxmt-artifacts/arm64ec/builtin/7/d3d11.dll"], b"clang-arm64ec")

        self.github.reset_mock()
        self.s3.reset_mock()
        self.syncer.backfill_builtin_builds(self.session, self.manager)
        self.github.get_run_artifacts.assert_not_called()
        restarted = ArtifactSyncer(self.github, self.engine, "test")
        restarted.backfill_builtin_builds(self.session, self.manager)
        self.github.get_run_artifacts.assert_called_once()
        self.github.download_artifact.assert_not_called()
        self.s3.upload_file.assert_not_called()
        self.assertEqual(len(self.artifacts()), 6)

    def test_backfill_retries_failures_after_other_runs(self):
        self.add_legacy_build(7)
        self.add_legacy_build(8)
        self.syncer.BACKFILL_BATCH_SIZE = 1
        self.s3.upload_file.side_effect = OSError("upload interrupted")
        with self.assertLogs("app.syncer", level="ERROR"):
            self.syncer.backfill_builtin_builds(self.session, self.manager)
        self.assertEqual({a.artifact_id for a in self.artifacts(8)}, {900})
        self.assertEqual(self.session.get(BuiltinBuild, 8).artifact_count, 2)
        self.s3.upload_file.side_effect = self.upload
        self.syncer.backfill_builtin_builds(self.session, self.manager)
        self.assertEqual(len(self.artifacts(7)), 6)
        self.assertEqual(len(self.artifacts(8)), 2)
        self.syncer.backfill_builtin_builds(self.session, self.manager)
        self.assertEqual(len(self.artifacts(8)), 6)
        self.assertEqual([call.args[2] for call in self.github.get_run_artifacts.call_args_list], [8, 7, 8])

    def test_backfill_database_failure_does_not_mark_sources_complete(self):
        self.add_legacy_build()
        with patch.object(self.session, "commit", side_effect=RuntimeError("commit interrupted")):
            with self.assertLogs("app.syncer", level="ERROR"):
                self.syncer.backfill_builtin_builds(self.session, self.manager)
        self.assertEqual({a.artifact_id for a in self.artifacts()}, {900})
        self.assertEqual(len(self.artifacts()), 2)
        self.syncer.backfill_builtin_builds(self.session, self.manager)
        self.assertEqual(len(self.artifacts()), 6)

    def test_backfill_preserves_missing_and_expired_sources(self):
        self.add_legacy_build()
        self.sources = [self.sources[0], source(11, self.sources[1].name, expired=True)]
        self.syncer.backfill_builtin_builds(self.session, self.manager)
        self.assertEqual(len(self.artifacts()), 4)
        self.assertEqual(self.objects["dxmt-artifacts/wow64/builtin/7/d3d11.dll"], b"old-gcc")
        self.assertEqual(self.objects["dxmt-artifacts/builtin/7/d3d11.dll"], b"clang-x64")
        self.assertEqual(self.objects["dxmt-artifacts/builtin/7/d3d12.dll"], b"d3d12")
        self.assertFalse(any(a.is_arm64ec for a in self.artifacts()))
        self.assertEqual([call.args[3] for call in self.github.download_artifact.call_args_list], [10])

    def test_fully_expired_build_is_checked_only_once_per_pass(self):
        self.add_legacy_build()
        self.sources = [source(a.id, a.name, expired=True) for a in self.sources]
        self.syncer.backfill_builtin_builds(self.session, self.manager)
        self.syncer.backfill_builtin_builds(self.session, self.manager)
        self.github.get_run_artifacts.assert_called_once()
        self.github.download_artifact.assert_not_called()
        self.assertEqual(len(self.artifacts()), 2)
        self.assertEqual(set(self.objects.values()), {b"old-gcc"})

    def test_missing_d3d12_in_old_clang_build_does_not_trigger_redownload(self):
        self.add_legacy_build()
        del self.archives[10]["x86_64-windows/d3d12.dll"]
        self.syncer.backfill_builtin_builds(self.session, self.manager)
        self.assertNotIn("d3d12.dll", [a.name for a in self.artifacts()])
        self.github.download_artifact.reset_mock()
        restarted = ArtifactSyncer(self.github, self.engine, "test")
        restarted.backfill_builtin_builds(self.session, self.manager)
        self.github.download_artifact.assert_not_called()

    def test_deleted_run_is_skipped_without_losing_existing_files(self):
        self.add_legacy_build()
        response = Response()
        response.status_code = 404
        self.github.get_run_artifacts.side_effect = HTTPError(response=response)
        self.syncer.backfill_builtin_builds(self.session, self.manager)
        self.syncer.backfill_builtin_builds(self.session, self.manager)
        self.github.get_run_artifacts.assert_called_once()
        self.assertEqual(len(self.artifacts()), 2)
        self.assertEqual(set(self.objects.values()), {b"old-gcc"})

    def test_temporary_github_failure_is_retried(self):
        self.add_legacy_build()
        response = Response()
        response.status_code = 503
        self.github.get_run_artifacts.side_effect = [
            HTTPError(response=response),
            SimpleNamespace(total_count=len(self.sources), artifacts=self.sources),
        ]
        with self.assertLogs("app.syncer", level="ERROR"):
            self.syncer.backfill_builtin_builds(self.session, self.manager)
        self.github.download_artifact.assert_not_called()
        self.assertEqual(len(self.artifacts()), 2)
        self.syncer.backfill_builtin_builds(self.session, self.manager)
        self.assertEqual(len(self.artifacts()), 6)

    def test_artifact_expiring_during_download_preserves_that_architecture(self):
        self.add_legacy_build()
        response = Response()
        response.status_code = 410
        def download(path, owner, repo, artifact_id):
            if artifact_id == 10:
                raise HTTPError(response=response)
            self.download(path, owner, repo, artifact_id)
        self.github.download_artifact.side_effect = download
        self.syncer.backfill_builtin_builds(self.session, self.manager)
        self.assertEqual(self.objects["dxmt-artifacts/builtin/7/d3d11.dll"], b"old-gcc")
        self.assertEqual(self.objects["dxmt-artifacts/wow64/builtin/7/d3d11.dll"], b"clang-i386")
        self.assertEqual(self.objects["dxmt-artifacts/arm64ec/builtin/7/d3d11.dll"], b"clang-arm64ec")
        self.assertEqual(len(self.artifacts()), 4)

    def test_sync_cycle_polls_new_runs_and_batches_old_builds(self):
        self.add_legacy_build(7)
        self.github.get_action_runs.return_value = SimpleNamespace(workflow_runs=[run(8)])
        self.github.get_releases.return_value = []
        self.syncer.BACKFILL_BATCH_SIZE = 1
        with patch("app.artifact_manager.boto3.client", return_value=self.s3):
            self.syncer._run_sync_cycle()
            self.assertEqual(len(self.artifacts(8)), 6)
            self.assertEqual(len(self.artifacts(7)), 2)
            self.syncer._run_sync_cycle()
        self.session.expire_all()
        self.assertEqual(len(self.artifacts(7)), 6)
        self.assertEqual(self.github.get_action_runs.call_count, 2)
        self.assertEqual(self.github.get_releases.call_count, 2)


    def test_selects_only_available_clang_releases(self):
        noise = [source(i, name) for i, name in enumerate((
            "dxmt-commit-7", "gcc-release-x86_64-windows-cross", "gcc-release-x86-windows-cross",
            "artifacts-release-gcc", "artifacts-release-wow64-gcc",
            "clang-debugoptimized-x86_64-windows-cross", "clang-release-arm64-macos",
        ), 1)]
        newest_x64 = source(20, self.sources[0].name)
        selected = self.syncer._select_builtin_source_artifacts([
            newest_x64, *noise, *self.sources, source(21, self.sources[0].name, expired=True),
        ])
        self.assertEqual([a.id for a in selected], [20, 11, 12])
        self.sources = noise
        self.syncer._process_builtin_run(run(), self.session, self.manager)
        self.assertIsNone(self.session.get(BuiltinBuild, 7))
        self.github.download_artifact.assert_not_called()
        self.s3.upload_file.assert_not_called()

    def test_imports_all_architectures_and_d3d12_without_redownloading(self):
        self.syncer._process_builtin_run(run(), self.session, self.manager)
        self.assertEqual(len(self.artifacts()), 6)
        build = self.session.get(BuiltinBuild, 7)
        self.assertEqual(build.artifact_count, 6)
        self.assertTrue(build.has_wow64)
        self.assertEqual(self.objects, {
            "dxmt-artifacts/builtin/7/d3d11.dll": b"clang-x64",
            "dxmt-artifacts/builtin/7/d3d12.dll": b"d3d12",
            "dxmt-artifacts/builtin/7/winemetal.so": b"unix-x64",
            "dxmt-artifacts/wow64/builtin/7/d3d11.dll": b"clang-i386",
            "dxmt-artifacts/arm64ec/builtin/7/d3d11.dll": b"clang-arm64ec",
            "dxmt-artifacts/arm64ec/builtin/7/winemetal.so": b"unix-arm64",
        })
        self.assertEqual({(a.name, a.is_wow64, a.is_arm64ec) for a in self.artifacts() if a.name == "d3d11.dll"}, {
            ("d3d11.dll", False, False), ("d3d11.dll", True, False), ("d3d11.dll", False, True),
        })
        self.github.download_artifact.reset_mock()
        self.s3.upload_file.reset_mock()
        self.syncer._process_builtin_run(run(), self.session, self.manager)
        self.github.download_artifact.assert_not_called()
        self.s3.upload_file.assert_not_called()
        self.assertEqual(len(self.artifacts()), 6)

    def test_artifact_listing_is_paginated(self):
        self.github.get_run_artifacts.side_effect = [
            SimpleNamespace(total_count=3, artifacts=self.sources[:1]),
            SimpleNamespace(total_count=3, artifacts=self.sources[1:]),
        ]
        self.syncer._process_builtin_run(run(), self.session, self.manager)
        self.assertEqual([call.kwargs["page"] for call in self.github.get_run_artifacts.call_args_list], [1, 2])
        self.assertEqual(len(self.artifacts()), 6)

    def test_failed_upload_does_not_record_completed_sources(self):
        self.s3.upload_file.side_effect = [None, OSError("upload interrupted")]
        with self.assertRaisesRegex(OSError, "upload interrupted"):
            self.syncer._process_builtin_run(run(), self.session, self.manager)
        self.assertIsNone(self.session.get(BuiltinBuild, 7))
        self.assertEqual(self.artifacts(), [])
        self.s3.upload_file.side_effect = self.upload
        self.syncer._process_builtin_run(run(), self.session, self.manager)
        self.assertEqual(len(self.artifacts()), 6)
        self.assertEqual({a.artifact_id for a in self.artifacts()}, {10, 11, 12})

    def test_empty_runtime_archive_can_be_retried(self):
        self.archives[10] = {"README.txt": b"no binaries"}
        with self.assertRaisesRegex(ValueError, "no runtime files"):
            self.syncer._process_builtin_run(run(), self.session, self.manager)
        self.assertEqual(self.artifacts(), [])
        self.archives[10] = {"x86_64-windows/d3d12.dll": b"d3d12"}
        self.syncer._process_builtin_run(run(), self.session, self.manager)
        self.assertIn("d3d12.dll", [a.name for a in self.artifacts()])

    def test_release_import_keeps_original_architectures(self):
        def download_release(path, *args):
            with tarfile.open(path, "w:gz") as archive:
                for name, data in (
                    ("v1/x86_64-windows/d3d11.dll", b"release-x64"),
                    ("v1/i386-windows/d3d11.dll", b"release-i386"),
                    ("v1/aarch64-windows/d3d11.dll", b"ignored-arm"),
                ):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
        self.github.download_release_asset.side_effect = download_release
        release = SimpleNamespace(
            tag_name="v1", created_at=datetime.now(timezone.utc),
            assets=[SimpleNamespace(id=1, name="dxmt-v1-builtin.tar.gz")],
        )
        self.syncer._process_release(release, self.session, self.manager)
        artifacts = self.session.exec(select(ReleaseArtifact)).all()
        self.assertEqual(sorted(a.is_wow64 for a in artifacts), [False, True])
        self.assertEqual(self.objects, {
            "dxmt-artifacts/release/v1/d3d11.dll": b"release-x64",
            "dxmt-artifacts/wow64/release/v1/d3d11.dll": b"release-i386",
        })


if __name__ == "__main__":
    unittest.main()
