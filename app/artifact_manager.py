from sqlmodel import Session, select, col
from typing import List, Union, Optional
from sqlalchemy import literal, cast, String, union_all, text
from sqlalchemy.orm import selectinload
import boto3


from .models.builds import BuiltinBuild, ReleaseBuild, BuiltinArtifact, ReleaseArtifact


class DXMTArtifactManager:
    def __init__(self, db_session: Session, bucket_name: str, bucket_prefix: str = "dxmt-artifacts/", endpoint_url: Optional[str] = None):
        self.db_session = db_session
        self.bucket_name = bucket_name
        self.bucket_prefix = bucket_prefix
        self.bucket_url = f"s3://{bucket_name}/{bucket_prefix}"
        # access keys are picked up from environment variables
        self.s3_client = boto3.client("s3", endpoint_url=endpoint_url) if endpoint_url else boto3.client("s3")


    def _get_s3_key(self, artifact: Union[BuiltinArtifact, ReleaseArtifact]) -> str:
        if isinstance(artifact, BuiltinArtifact):
            return f"{self.bucket_prefix}builtin/{artifact.build_id}/{artifact.name}"
        elif isinstance(artifact, ReleaseArtifact):
            return f"{self.bucket_prefix}release/{artifact.build_tag}/{artifact.name}"
        raise ValueError(f"Unknown artifact type: {type(artifact)}")

    def get_presigned_url(self, artifact: Union[BuiltinArtifact, ReleaseArtifact], expiration: int = 3600) -> str:
        key = self._get_s3_key(artifact)
        return self.s3_client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket_name,
                "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{artifact.name}"',
            },
            ExpiresIn=expiration,
        )


    def list_builds(self, page: int = 1, page_size: int = 10) -> List[Union[BuiltinBuild, ReleaseBuild]]:
        offset = (page - 1) * page_size

        q_builtin = select(
            literal("builtin").label("type"),
            cast(col(BuiltinBuild.github_run_id), String).label("id"),
            col(BuiltinBuild.created_at),
        )

        q_release = select(
            literal("release").label("type"),
            col(ReleaseBuild.tag).label("id"),
            col(ReleaseBuild.created_at),
        )

        combined_query = (
            union_all(q_builtin, q_release)
            .order_by(text("created_at DESC"))
            .limit(page_size)
            .offset(offset)
        )

        results = self.db_session.exec(combined_query).all()

        builtin_ids = [int(row.id) for row in results if row.type == "builtin"]
        release_ids = [row.id for row in results if row.type == "release"]

        builds = []
        if builtin_ids:
            builds.extend(
                self.db_session.exec(
                    select(BuiltinBuild)
                    .where(col(BuiltinBuild.github_run_id).in_(builtin_ids))
                    .options(selectinload(BuiltinBuild.artifacts))
                ).all()
            )
        if release_ids:
            builds.extend(
                self.db_session.exec(
                    select(ReleaseBuild)
                    .where(col(ReleaseBuild.tag).in_(release_ids))
                    .options(selectinload(ReleaseBuild.artifacts))
                ).all()
            )

        builds.sort(key=lambda b: b.created_at, reverse=True)

        return builds

    def list_artifacts(
        self,
        tag: Optional[str] = None,
        id: Optional[int] = None,
        commit_sha: Optional[str] = None,
    ) -> List[Union[BuiltinArtifact, ReleaseArtifact]]:
        if tag:
            # List artifacts for a release build by tag
            return list(self.db_session.exec(
                select(ReleaseArtifact).where(ReleaseArtifact.build_tag == tag)
            ).all())
        elif id:
            # List artifacts for a builtin build by github_run_id
            return list(self.db_session.exec(
                select(BuiltinArtifact).where(BuiltinArtifact.build_id == id)
            ).all())
        elif commit_sha:
            # List artifacts for a builtin build by commit_sha
            # First find the build_id from the commit_sha
            build = self.db_session.exec(
                select(BuiltinBuild).where(BuiltinBuild.commit_sha == commit_sha)
            ).first()
            if build:
                return list(self.db_session.exec(
                    select(BuiltinArtifact).where(BuiltinArtifact.build_id == build.github_run_id)
                ).all())
            return []
        return []
