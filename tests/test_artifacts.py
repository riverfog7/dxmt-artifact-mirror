from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

from app.artifact_manager import DXMTArtifactManager
from app.models.builds import BuiltinArtifact, BuiltinBuild, ReleaseArtifact, ReleaseBuild
from app.errors import install_error_handlers
from app.models.api import BuildDetail, BuildPage, ErrorResponse
from app.router import build_router, get_artifact_manager, router
from app.utils import init_db


class DatabaseMigrationTests(unittest.TestCase):
    def test_populated_database_migration_is_repeatable(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine(f"sqlite:///{Path(directory) / 'legacy.db'}")
            with engine.begin() as connection:
                connection.execute(text("""
                    CREATE TABLE builtinbuild (
                        github_run_id INTEGER PRIMARY KEY, commit_sha TEXT NOT NULL,
                        description TEXT NOT NULL, created_at DATETIME NOT NULL,
                        artifact_count INTEGER NOT NULL, has_wow64 BOOLEAN NOT NULL
                    )
                """))
                connection.execute(text("""
                    CREATE TABLE builtinartifact (
                        id INTEGER PRIMARY KEY, artifact_id INTEGER NOT NULL,
                        build_id INTEGER NOT NULL, name TEXT NOT NULL, is_wow64 BOOLEAN NOT NULL
                    )
                """))
                connection.execute(text("""
                    INSERT INTO builtinbuild VALUES (7, 'abc', 'old build', '2026-01-01', 2, TRUE)
                """))
                connection.execute(text("""
                    INSERT INTO builtinartifact VALUES
                    (1, 10, 7, 'd3d11.dll', FALSE), (2, 11, 7, 'd3d11.dll', TRUE)
                """))

            init_db(engine)
            init_db(engine)
            with Session(engine) as session:
                artifacts = session.exec(select(BuiltinArtifact).order_by(BuiltinArtifact.id)).all()
                self.assertEqual([(a.id, a.is_wow64, a.is_arm64ec) for a in artifacts], [
                    (1, False, False), (2, True, False),
                ])
                self.assertEqual(session.get(BuiltinBuild, 7).artifact_count, 2)
            with engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO builtinartifact (artifact_id, build_id, name, is_wow64)
                    VALUES (12, 7, 'dxgi.dll', FALSE)
                """))
                self.assertEqual(connection.execute(text(
                    "SELECT is_arm64ec FROM builtinartifact WHERE artifact_id = 12"
                )).scalar_one(), 0)
            engine.dispose()

    def test_new_database_has_a_nonnullable_false_default(self):
        engine = create_engine("sqlite://")
        init_db(engine)
        column = next(c for c in inspect(engine).get_columns("builtinartifact") if c["name"] == "is_arm64ec")
        self.assertFalse(column["nullable"])
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO builtinartifact (artifact_id, build_id, name, is_wow64)
                VALUES (1, 7, 'd3d11.dll', FALSE)
            """))
            self.assertEqual(connection.execute(text("SELECT is_arm64ec FROM builtinartifact")).scalar_one(), 0)
        engine.dispose()


class ArtifactAPITests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        init_db(self.engine)
        self.session = Session(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.session.close)
        self.commit = "abcdef01" * 5
        for run_id in (7, 8):
            self.session.add(BuiltinBuild(
                github_run_id=run_id, commit_sha=self.commit, description="build",
                created_at=datetime(2026, 10, 25, tzinfo=timezone.utc), artifact_count=999,
            ))
        for index, (wow64, arm64ec) in enumerate(((False, False), (True, False), (False, True)), 1):
            self.session.add(BuiltinArtifact(
                artifact_id=100+index, build_id=7, name="d3d11.dll", is_wow64=wow64, is_arm64ec=arm64ec,
            ))
        self.session.add(BuiltinArtifact(artifact_id=200, build_id=7, name="d3d12.dll"))
        self.session.add(BuiltinArtifact(artifact_id=201, build_id=8, name="d3d11.dll"))
        self.session.add(ReleaseBuild(tag="v0.80", created_at=datetime(2026, 10, 24, tzinfo=timezone.utc), artifact_count=2))
        self.session.add(ReleaseBuild(tag="v0.81", created_at=datetime(2026, 10, 26, tzinfo=timezone.utc), artifact_count=0))
        for wow64 in (False, True):
            self.session.add(ReleaseArtifact(build_tag="v0.80", name="d3d11.dll", is_wow64=wow64))
        self.session.commit()
        self.s3 = Mock()
        self.s3.generate_presigned_url.side_effect = lambda operation, Params, ExpiresIn: (
            f"https://storage.test/{Params['Key']}"
        )
        with patch("app.artifact_manager.boto3.client", return_value=self.s3):
            self.manager = DXMTArtifactManager(self.session, "test")
        self.api = FastAPI()
        self.api.include_router(router)
        self.api.include_router(build_router)
        install_error_handlers(self.api)
        self.api.dependency_overrides[get_artifact_manager] = lambda: self.manager
        self.client = TestClient(self.api)
        self.addCleanup(self.client.close)

    def assert_error(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        error = ErrorResponse.model_validate(response.json()).error
        self.assertEqual(error.code, code)
        return error

    def test_list_has_consistent_public_models_and_pagination(self):
        response = self.client.get("/builds", params={"per_page": 2})
        self.assertEqual(response.status_code, 200, response.text)
        result = BuildPage.model_validate(response.json())
        self.assertEqual((result.total, result.page, result.per_page, result.has_more), (4, 1, 2, True))
        self.assertEqual([(b.source.value, b.ref) for b in result.items], [("release", "v0.81"), ("ci", "8")])
        second = self.client.get("/builds", params={"per_page": 2, "page": 2}).json()
        self.assertEqual([(b["source"], b["ref"]) for b in second["items"]], [("ci", "7"), ("release", "v0.80")])
        self.assertFalse(second["has_more"])
        self.assertEqual(second["items"][0]["file_count"], 4)
        for build in response.json()["items"] + second["items"]:
            self.assertEqual(set(build), {"source", "ref", "created_at", "commit_sha", "description", "architectures", "file_count"})
            self.assertTrue(build["created_at"].endswith("Z"))
        beyond = self.client.get("/builds", params={"page": 100}).json()
        self.assertEqual((beyond["items"], beyond["total"], beyond["has_more"]), ([], 4, False))

    def test_architecture_filters_use_available_files_without_duplicates(self):
        for arch, expected in (
            ("x86", [("ci", "7"), ("release", "v0.80")]),
            ("x86_64", [("ci", "8"), ("ci", "7"), ("release", "v0.80")]),
            ("arm64ec", [("ci", "7")]),
        ):
            response = self.client.get("/builds", params={"arch": arch})
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual([(b["source"], b["ref"]) for b in result["items"]], expected)
            self.assertEqual(result["total"], len(expected))
        self.assertEqual(self.client.get("/builds", params={"source": "release", "arch": "arm64ec"}).json()["items"], [])

    def test_commit_search_returns_every_matching_run_in_deterministic_order(self):
        for params in ({"commit": self.commit.upper()}, {"commit": self.commit, "source": "ci"}):
            result = self.client.get("/builds", params=params)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual([b["ref"] for b in result.json()["items"]], ["8", "7"])
        self.assertEqual(self.client.get("/builds", params={"commit": "0"*40}).json()["items"], [])

    def test_details_show_all_architectures_and_optional_file_filter(self):
        response = self.client.get("/builds/ci/7")
        self.assertEqual(response.status_code, 200, response.text)
        build = BuildDetail.model_validate(response.json())
        self.assertEqual({a.value for a in build.architectures}, {"x86", "x86_64", "arm64ec"})
        self.assertEqual(len(build.files), 4)
        self.assertEqual({a.name for a in build.files}, {"d3d11.dll", "d3d12.dll"})
        selected = self.client.get("/builds/ci/7", params={"arch": "arm64ec"}).json()
        self.assertEqual(selected["files"], [{"name": "d3d11.dll", "arch": "arm64ec"}])
        self.assertEqual(selected["file_count"], 4)
        release = self.client.get("/builds/release/v0.80").json()
        self.assertEqual(set(release), set(response.json()))
        self.assertIsNone(release["commit_sha"])
        self.assertEqual(release["architectures"], ["x86", "x86_64"])
        self.assertEqual(self.client.get("/builds/release/v0.80?arch=arm64ec").json()["files"], [])
        self.s3.generate_presigned_url.assert_not_called()

    def test_downloads_keep_architectures_separate(self):
        for arch, prefix in (("x86_64", ""), ("x86", "wow64/"), ("arm64ec", "arm64ec/")):
            with self.subTest(arch=arch):
                response = self.client.get(f"/builds/ci/7/files/{arch}/d3d11.dll", follow_redirects=False)
                self.assertEqual(response.status_code, 307)
                self.assertEqual(response.headers["location"], f"https://storage.test/dxmt-artifacts/{prefix}builtin/7/d3d11.dll")
                self.assertEqual(response.headers["cache-control"], "no-store")
        for arch, prefix in (("x86", "wow64/"), ("x86_64", "")):
            response = self.client.get(f"/builds/release/v0.80/files/{arch}/d3d11.dll", follow_redirects=False)
            self.assertEqual(response.headers["location"], f"https://storage.test/dxmt-artifacts/{prefix}release/v0.80/d3d11.dll")

    def test_validation_and_missing_resources_use_typed_errors(self):
        for params in (
            {"page": 0}, {"per_page": 0}, {"per_page": 101}, {"source": "builtin"},
            {"arch": "arm64"}, {"commit": "abc"}, {"wow64": True},
            {"source": "release", "commit": self.commit},
        ):
            with self.subTest(params=params):
                error = self.assert_error(self.client.get("/builds", params=params), 422, "invalid_request")
                self.assertTrue(error.details)
        self.assert_error(self.client.get("/builds/ci/999"), 404, "build_not_found")
        self.assert_error(self.client.get("/builds/release/missing"), 404, "build_not_found")
        self.assert_error(self.client.get("/builds/ci/7/files/x86_64/missing.dll"), 404, "file_not_found")
        self.assert_error(self.client.get("/builds/release/v0.80/files/arm64ec/d3d11.dll"), 404, "file_not_found")
        self.assert_error(self.client.get("/builds/ci/7/files/arm64/d3d11.dll"), 422, "invalid_request")
        self.assert_error(self.client.get("/builds/ci/7?arm64ec=true"), 422, "invalid_request")
        self.assert_error(self.client.get("/builds/ci/7/files/x86/d3d11.dll?wow64=true"), 422, "invalid_request")
        self.assert_error(self.client.post("/builds"), 405, "method_not_allowed")
        self.assert_error(self.client.get("/builds/list"), 422, "invalid_request")
        self.assert_error(self.client.get("/artifacts/list"), 404, "not_found")
        self.assert_error(self.client.get("/builds/ci/" + "9"*100), 404, "build_not_found")

    def test_unexpected_errors_have_the_public_error_shape(self):
        with TestClient(self.api, raise_server_exceptions=False) as client:
            with patch.object(self.manager, "get_build", side_effect=RuntimeError("private database details")):
                with self.assertLogs("app.errors", level="ERROR"):
                    response = client.get("/builds/ci/7")
        self.assert_error(response, 500, "internal_error")
        self.assertNotIn("private database details", response.text)

    def test_openapi_describes_models_enums_and_errors(self):
        spec = self.client.get("/openapi.json").json()
        self.assertEqual(spec["components"]["schemas"]["Architecture"]["enum"], ["x86", "x86_64", "arm64ec"])
        self.assertEqual(spec["components"]["schemas"]["BuildSource"]["enum"], ["ci", "release"])
        operation = spec["paths"]["/builds"]["get"]
        for status, model in (("200", "BuildPage"), ("422", "ErrorResponse")):
            self.assertEqual(operation["responses"][status]["content"]["application/json"]["schema"]["$ref"], f"#/components/schemas/{model}")
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})

    def test_s3_prefixes_compose_independently(self):
        for wow64, arm64ec, prefix in (
            (False, False, ""), (True, False, "wow64/"),
            (False, True, "arm64ec/"), (True, True, "arm64ec/wow64/"),
        ):
            artifact = BuiltinArtifact(artifact_id=1, build_id=7, name="d3d11.dll", is_wow64=wow64, is_arm64ec=arm64ec)
            self.assertEqual(self.manager._get_s3_key(artifact), f"dxmt-artifacts/{prefix}builtin/7/d3d11.dll")


if __name__ == "__main__":
    unittest.main()
