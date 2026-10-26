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
from app.router import artifact_router, build_router, get_artifact_manager
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
        self.session.add(BuiltinBuild(
            github_run_id=7, commit_sha="abc", description="build", created_at=datetime.now(timezone.utc),
            artifact_count=3, has_wow64=True,
        ))
        for index, (wow64, arm64ec) in enumerate(((False, False), (True, False), (False, True)), 1):
            self.session.add(BuiltinArtifact(
                artifact_id=100+index, build_id=7, name="d3d11.dll", is_wow64=wow64, is_arm64ec=arm64ec,
            ))
        self.session.add(ReleaseBuild(tag="v1", created_at=datetime.now(timezone.utc), artifact_count=1))
        self.session.add(ReleaseArtifact(build_tag="v1", name="d3d11.dll"))
        self.session.commit()
        self.s3 = Mock()
        self.s3.generate_presigned_url.side_effect = lambda operation, Params, ExpiresIn: (
            f"https://storage.test/{Params['Key']}"
        )
        with patch("app.artifact_manager.boto3.client", return_value=self.s3):
            self.manager = DXMTArtifactManager(self.session, "test")
        api = FastAPI()
        api.include_router(artifact_router)
        api.include_router(build_router)
        api.dependency_overrides[get_artifact_manager] = lambda: self.manager
        self.client = TestClient(api)
        self.addCleanup(self.client.close)

    def test_list_filters_both_flags_by_id_and_commit(self):
        for identifier in ({"id": 7}, {"commit_sha": "abc"}):
            for wow64, arm64ec in ((False, False), (True, False), (False, True), (True, True)):
                with self.subTest(identifier=identifier, wow64=wow64, arm64ec=arm64ec):
                    response = self.client.get("/artifacts/list", params={
                        **identifier, "wow64": wow64, "arm64ec": arm64ec,
                    })
                    self.assertEqual(response.status_code, 200)
                    artifacts = response.json()["artifacts"]
                    self.assertEqual(len(artifacts), 0 if wow64 and arm64ec else 1)
                    for artifact in artifacts:
                        self.assertEqual((artifact["is_wow64"], artifact["is_arm64ec"]), (wow64, arm64ec))
        default = self.client.get("/artifacts/list", params={"id": 7}).json()["artifacts"]
        self.assertEqual([(a["is_wow64"], a["is_arm64ec"]) for a in default], [(False, False)])

    def test_downloads_keep_architectures_separate(self):
        for params, prefix in (({}, ""), ({"wow64": True}, "wow64/"), ({"arm64ec": True}, "arm64ec/")):
            with self.subTest(params=params):
                response = self.client.get("/builds/download/7/artifact/d3d11.dll", params=params, follow_redirects=False)
                self.assertEqual(response.status_code, 307)
                self.assertEqual(response.headers["location"], f"https://storage.test/dxmt-artifacts/{prefix}builtin/7/d3d11.dll")
        response = self.client.get("/builds/download/7/artifact/d3d11.dll", params={"wow64": True, "arm64ec": True})
        self.assertEqual(response.status_code, 404)

    def test_s3_prefixes_compose_independently(self):
        for wow64, arm64ec, prefix in (
            (False, False, ""), (True, False, "wow64/"),
            (False, True, "arm64ec/"), (True, True, "arm64ec/wow64/"),
        ):
            artifact = BuiltinArtifact(artifact_id=1, build_id=7, name="d3d11.dll", is_wow64=wow64, is_arm64ec=arm64ec)
            self.assertEqual(self.manager._get_s3_key(artifact), f"dxmt-artifacts/{prefix}builtin/7/d3d11.dll")

    def test_tagged_release_defaults_are_preserved(self):
        self.assertEqual(len(self.client.get("/artifacts/list", params={"tag": "v1"}).json()["artifacts"]), 1)
        self.assertEqual(self.client.get("/artifacts/list", params={"tag": "v1", "arm64ec": True}).json()["artifacts"], [])
        response = self.client.get("/artifacts/download/v1/artifact/d3d11.dll", follow_redirects=False)
        self.assertEqual(response.headers["location"], "https://storage.test/dxmt-artifacts/release/v1/d3d11.dll")


if __name__ == "__main__":
    unittest.main()
