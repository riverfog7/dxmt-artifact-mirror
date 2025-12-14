from datetime import datetime
from typing import List, Optional

from sqlmodel import Field, Relationship, SQLModel
from pydantic import field_validator


class BuiltinBuild(SQLModel, table=True):
    __tablename__ = "builtinbuild"

    id: int = Field(primary_key=True)  # the github action run ID
    commit_sha: str = Field(index=True)
    description: str
    created_at: datetime
    artifact_count: int

    artifacts: List["BuiltinArtifact"] = Relationship(back_populates="build")


class ReleaseBuild(SQLModel, table=True):
    __tablename__ = "releasebuild"
    tag: str = Field(primary_key=True, index=True)  # the release tag
    created_at: datetime
    artifact_count: int

    artifacts: List["ReleaseArtifact"] = Relationship(back_populates="build")


class BuiltinArtifact(SQLModel, table=True):
    # This represents a file artifact produced by a built-in build.
    # one build can have multiple files (dll, so, etc)
    __tablename__ = "builtinartifact"

    id: Optional[int] = Field(default=None, primary_key=True)
    artifact_id: int = Field(index=True)  # corresponds to GitHub artifact ID can be non-unique
    build_id: int = Field(foreign_key="builtinbuild.id", index=True)
    name: str  # file name without any path components

    build: BuiltinBuild = Relationship(back_populates="artifacts")

    @field_validator("name")
    def validate_name(cls, v):
        if "/" in v or "\\" in v:
            raise ValueError("Artifact name must not contain path components")
        return v


class ReleaseArtifact(SQLModel, table=True):
    # This represents a file artifact produced by a built-in build.
    # one build can have multiple files (dll, so, etc)
    __tablename__ = "releaseartifact"
    id: Optional[int] = Field(default=None, primary_key=True)
    build_tag: str = Field(foreign_key="releasebuild.tag", index=True)
    name: str  # file name without any path components

    build: ReleaseBuild = Relationship(back_populates="artifacts")

    @field_validator("name")
    def validate_name(cls, v):
        if "/" in v or "\\" in v:
            raise ValueError("Artifact name must not contain path components")
        return v
