from datetime import datetime
from typing import List, Optional

from sqlmodel import Field, Relationship, SQLModel
from pydantic import field_validator


class BuiltinBuild(SQLModel, table=True):
    __tablename__ = "builtinbuild"

    id: int
    commit_sha: str = Field(index=True)
    description: str
    created_at: datetime
    artifact_count: int

    artifacts: List["Artifact"] = Relationship(back_populates="build")


class Artifact(SQLModel, table=True):
    __tablename__ = "artifact"

    id: int
    build_id: Optional[int] = Field(default=None, foreign_key="builtinbuild.id", index=True)
    name: str  # file name without any path components

    build: Optional["BuiltinBuild"] = Relationship(back_populates="artifacts")

    @field_validator("name")
    def validate_name(cls, v):
        if "/" in v or "\\" in v:
            raise ValueError("Artifact name must not contain path components")
        return v
