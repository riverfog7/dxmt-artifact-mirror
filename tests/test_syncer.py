from datetime import datetime, timezone
import io
from pathlib import Path
from types import SimpleNamespace
import tarfile
import unittest
from unittest.mock import Mock, patch
import zipfile

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
