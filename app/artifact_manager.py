from sqlmodel import Session, select, col
from typing import List, Union, Optional
from sqlalchemy import and_, false, func, literal, cast, String, union_all
from sqlalchemy.orm import selectinload
import boto3


from .models.builds import BuiltinBuild, ReleaseBuild, BuiltinArtifact, ReleaseArtifact
from .models.api import Architecture, BuildFilters, BuildListQuery, BuildSource


class DXMTArtifactManager:
    def __init__(self, db_session: Session, bucket_name: str, bucket_prefix: str = "dxmt-artifacts/", endpoint_url: Optional[str] = None):
        self.db_session = db_session
        self.bucket_name = bucket_name
        self.bucket_prefix = bucket_prefix
        self.bucket_url = f"s3://{bucket_name}/{bucket_prefix}"
        # access keys are picked up from environment variables
        if endpoint_url:
            self.s3_client = boto3.client("s3", endpoint_url=endpoint_url, config=boto3.session.Config(s3={'addressing_style': 'path'}))
        else:
            self.s3_client = boto3.client("s3")


    def _get_s3_key(self, artifact: Union[BuiltinArtifact, ReleaseArtifact]) -> str:
        prefix = "wow64/" if artifact.is_wow64 else ""
        if isinstance(artifact, BuiltinArtifact):
            if artifact.is_arm64ec:
                prefix = f"arm64ec/{prefix}"
            return f"{self.bucket_prefix}{prefix}builtin/{artifact.build_id}/{artifact.name}"
        elif isinstance(artifact, ReleaseArtifact):
            return f"{self.bucket_prefix}{prefix}release/{artifact.build_tag}/{artifact.name}"
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


    @staticmethod
    def _architecture_filter(model, arch: Architecture):
        conditions = [model.is_wow64 == (arch == Architecture.X86)]
        if model is BuiltinArtifact:
            conditions.append(model.is_arm64ec == (arch == Architecture.ARM64EC))
        elif arch == Architecture.ARM64EC:
            conditions.append(false())
        return and_(*conditions)

    def _build_query(self, filters: BuildFilters, source: BuildSource | None):
        queries = []
        for kind, model, artifact_model, key, foreign_key in (
            (BuildSource.CI, BuiltinBuild, BuiltinArtifact, BuiltinBuild.github_run_id, BuiltinArtifact.build_id),
            (BuildSource.RELEASE, ReleaseBuild, ReleaseArtifact, ReleaseBuild.tag, ReleaseArtifact.build_tag),
        ):
            if (source is not None and source != kind) or (filters.commit and kind != BuildSource.CI):
                continue
            query = select(
                literal(kind.value).label("source"), cast(col(key), String).label("ref"),
                model.created_at, (col(key) if kind == BuildSource.CI else literal(0)).label("sort_id"),
            )
            if filters.commit:
                query = query.where(BuiltinBuild.commit_sha == filters.commit)
            if filters.arch is not None:
                files = select(artifact_model.id).where(
                    foreign_key == key, self._architecture_filter(artifact_model, filters.arch),
                )
                query = query.where(files.exists())
            queries.append(query)
        return union_all(*queries).subquery()

    @staticmethod
    def _ordered_builds(query):
        return select(query.c.source, query.c.ref, query.c.created_at, query.c.sort_id).order_by(
            query.c.created_at.desc(), query.c.source, query.c.sort_id.desc(), query.c.ref.desc(),
        )

    def _load_builds(self, rows) -> List[Union[BuiltinBuild, ReleaseBuild]]:
        builds = {}
        for kind, model, key in (
            (BuildSource.CI, BuiltinBuild, BuiltinBuild.github_run_id),
            (BuildSource.RELEASE, ReleaseBuild, ReleaseBuild.tag),
        ):
            refs = [int(row.ref) if kind == BuildSource.CI else row.ref for row in rows if row.source == kind]
            if not refs:
                continue
            for build in self.db_session.exec(
                select(model).where(col(key).in_(refs)).options(selectinload(model.artifacts))
            ).all():
                ref = str(build.github_run_id) if kind == BuildSource.CI else build.tag
                builds[(kind, ref)] = build
        return [builds[(row.source, row.ref)] for row in rows]

    def list_builds(self, filters: BuildListQuery) -> tuple[List[Union[BuiltinBuild, ReleaseBuild]], int]:
        query = self._build_query(filters, filters.source)
        total = self.db_session.exec(select(func.count()).select_from(query)).one()
        rows = self.db_session.exec(
            self._ordered_builds(query).limit(filters.per_page).offset((filters.page-1)*filters.per_page)
        ).all()
        return self._load_builds(rows), total

    def get_build(self, source: BuildSource, ref: str) -> BuiltinBuild | ReleaseBuild | None:
        if source == BuildSource.CI:
            if len(ref) > 19 or not ref.isascii() or not ref.isdecimal() or not 0 < int(ref) < 2**63:
                return None
            model, condition = BuiltinBuild, BuiltinBuild.github_run_id == int(ref)
        else:
            model, condition = ReleaseBuild, ReleaseBuild.tag == ref
        return self.db_session.exec(
            select(model).where(condition).options(selectinload(model.artifacts))
        ).first()

    def get_file(self, build: BuiltinBuild | ReleaseBuild, arch: Architecture, name: str) -> BuiltinArtifact | ReleaseArtifact | None:
        return next((a for a in build.artifacts if a.name == name and Architecture.of(a) == arch), None)
