from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional, Union

from pydantic import BaseModel

from .builds import BuiltinBuild, ReleaseBuild, BuiltinArtifact, ReleaseArtifact


class Artifact(BaseModel):
    name: str

    @classmethod
    def from_database_artifact(cls, db_artifact: Union['BuiltinArtifact', 'ReleaseArtifact']) -> Artifact:
        return cls(
            name=db_artifact.name
        )


class Build(BaseModel):
    type: Literal["builtin", "release"]
    artifact_count: int
    created_at: datetime

    # tag only for release builds
    tag: Optional[str] = None

    # commit sha and id, description only for builtin builds
    commit_sha: Optional[str] = None
    github_run_id: Optional[int] = None # the github action run ID
    description: Optional[str] = None

    @classmethod
    def from_database_build(cls, db_build: Union[BuiltinBuild, ReleaseBuild]) -> Build:
        if db_build.__class__.__name__ == "BuiltinBuild":
            return cls(
                type="builtin",
                artifact_count=db_build.artifact_count,
                created_at=db_build.created_at,
                commit_sha=db_build.commit_sha,
                github_run_id=db_build.github_run_id,
            )
        elif db_build.__class__.__name__ == "ReleaseBuild":
            return cls(
                type="release",
                artifact_count=db_build.artifact_count,
                created_at=db_build.created_at,
                tag=db_build.tag,
            )
        else:
            raise ValueError("Unknown build type")
