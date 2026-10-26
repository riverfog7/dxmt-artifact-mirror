from datetime import datetime, timezone
from enum import StrEnum
from typing import Annotated, Callable, Literal
from urllib.parse import quote

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from .builds import BuiltinArtifact, BuiltinBuild, ReleaseArtifact, ReleaseBuild

FileName = Annotated[str, StringConstraints(min_length=1, max_length=255, pattern=r"^[^/\\\x00]+$")]


class BuildSource(StrEnum):
    CI = "ci"
    RELEASE = "release"


class Architecture(StrEnum):
    X86 = "x86"
    X86_64 = "x86_64"
    ARM64EC = "arm64ec"

    @classmethod
    def of(cls, artifact: BuiltinArtifact | ReleaseArtifact) -> "Architecture":
        if getattr(artifact, "is_arm64ec", False):
            if artifact.is_wow64:
                raise ValueError("No public architecture represents both Wow64 and ARM64EC")
            return cls.ARM64EC
        return cls.X86 if artifact.is_wow64 else cls.X86_64


class BuildFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arch: Architecture | None = Field(default=None, description="Require files for this architecture; omitted means all architectures.")
    commit: str | None = Field(default=None, pattern=r"^[0-9a-fA-F]{40}$", description="Full CI commit SHA; matches every mirrored run for that commit.")

    @field_validator("commit")
    @classmethod
    def lowercase_commit(cls, value: str | None) -> str | None:
        return value.lower() if value else value


class BuildListQuery(BuildFilters):
    source: BuildSource | None = None
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def validate_commit_source(self) -> "BuildListQuery":
        if self.source == BuildSource.RELEASE and self.commit:
            raise ValueError("The commit filter applies to CI builds only")
        return self


class BuildDetailQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    arch: Architecture | None = None


class LatestBuildQuery(BuildDetailQuery):
    file: FileName | None = Field(default=None, description="Require this file in the selected architecture, or any architecture if arch is omitted.")


class EmptyQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BuildFile(BaseModel):
    name: FileName
    arch: Architecture
    download_url: AnyHttpUrl

    @classmethod
    def from_artifact(cls, artifact: BuiltinArtifact | ReleaseArtifact, download_url: str) -> "BuildFile":
        return cls(name=artifact.name, arch=Architecture.of(artifact), download_url=download_url)


class BuildSummary(BaseModel):
    source: BuildSource
    ref: str = Field(description="CI run ID or release tag.")
    url: AnyHttpUrl
    created_at: datetime
    commit_sha: str | None
    description: str | None
    architectures: list[Architecture]
    file_count: int = Field(ge=0)

    @field_validator("created_at")
    @classmethod
    def utc_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @classmethod
    def from_build(cls, build: BuiltinBuild | ReleaseBuild, url_for: Callable[..., str]) -> "BuildSummary":
        is_ci = isinstance(build, BuiltinBuild)
        source = BuildSource.CI if is_ci else BuildSource.RELEASE
        ref = str(build.github_run_id) if is_ci else build.tag
        return cls(
            source=source,
            ref=ref,
            url=url_for("get_build", source=source.value, ref=quote(ref, safe="")),
            created_at=build.created_at,
            commit_sha=build.commit_sha if is_ci else None,
            description=build.description if is_ci else None,
            architectures=sorted({Architecture.of(a) for a in build.artifacts}),
            file_count=len(build.artifacts),
        )


class BuildDetail(BuildSummary):
    files: list[BuildFile]

    @classmethod
    def from_build(
        cls, build: BuiltinBuild | ReleaseBuild, url_for: Callable[..., str], arch: Architecture | None = None,
    ) -> "BuildDetail":
        summary = BuildSummary.from_build(build, url_for)
        files = [BuildFile.from_artifact(a, download_url=url_for(
            "download_file", source=summary.source.value, ref=quote(summary.ref, safe=""),
            arch=Architecture.of(a).value, name=quote(a.name, safe=""),
        )) for a in build.artifacts if arch is None or Architecture.of(a) == arch]
        return cls(**summary.model_dump(), files=sorted(files, key=lambda f: (f.arch, f.name)))


class BuildPage(BaseModel):
    items: list[BuildSummary]
    page: int
    per_page: int
    total: int
    has_more: bool


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ValidationIssue(BaseModel):
    location: list[str | int]
    message: str
    type: str


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: list[ValidationIssue] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: ErrorDetail
