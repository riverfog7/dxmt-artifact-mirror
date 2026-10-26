from typing import Annotated

from fastapi import Depends, Path, Query, Request, Response
from fastapi.routing import APIRouter
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from .utils import get_db, get_bucket_name, get_endpoint_url
from .artifact_manager import DXMTArtifactManager
from .errors import APIError
from .models.api import (
    Architecture, BuildDetail, BuildDetailQuery, BuildListQuery, BuildPage,
    BuildSource, BuildSummary, EmptyQuery, ErrorResponse, FileName, HealthResponse, LatestBuildQuery,
)

router = APIRouter()
build_router = APIRouter(prefix="/builds", tags=["Builds"], responses={
    404: {"model": ErrorResponse, "description": "Build or file not found"},
    422: {"model": ErrorResponse, "description": "Invalid request"},
    500: {"model": ErrorResponse, "description": "Internal server error"},
})

def get_artifact_manager(session: Session = Depends(get_db)):
    return DXMTArtifactManager(session, bucket_name=get_bucket_name(), endpoint_url=get_endpoint_url())

@router.get("/health", response_model=HealthResponse, tags=["Health"])
def health_check() -> HealthResponse:
    return HealthResponse()


Manager = Annotated[DXMTArtifactManager, Depends(get_artifact_manager)]


def require_build(manager: DXMTArtifactManager, source: BuildSource, ref: str):
    build = manager.get_build(source, ref)
    if build is None:
        raise APIError(404, "build_not_found", f"No mirrored {source.value} build found for {ref}")
    return build


def require_latest_build(manager: DXMTArtifactManager, source: BuildSource, filters: LatestBuildQuery):
    build = manager.get_latest_build(source, filters)
    if build is None:
        raise APIError(404, "build_not_found", f"No mirrored {source.value} build matches the requested files")
    return build


@build_router.get("", response_model=BuildPage, summary="List mirrored builds")
def list_builds(request: Request, filters: Annotated[BuildListQuery, Query()], manager: Manager) -> BuildPage:
    builds, total = manager.list_builds(filters)
    return BuildPage(
        items=[BuildSummary.from_build(build, lambda route, **params: str(request.url_for(route, **params))) for build in builds], page=filters.page,
        per_page=filters.per_page, total=total, has_more=filters.page*filters.per_page < total,
    )


@build_router.get("/{source}/latest", response_model=BuildDetail, summary="Find the newest build with matching files")
def latest_build(
    request: Request, response: Response, source: BuildSource,
    filters: Annotated[LatestBuildQuery, Query()], manager: Manager,
) -> BuildDetail:
    build = require_latest_build(manager, source, filters)
    response.headers["Cache-Control"] = "no-store"
    return BuildDetail.from_build(build, lambda route, **params: str(request.url_for(route, **params)), filters.arch)


@build_router.get(
    "/{source}/{ref:path}/files/{arch}/{name}", name="download_file", response_class=RedirectResponse,
    summary="Download a mirrored file", status_code=307,
    responses={307: {"description": "Redirect to a temporary signed storage URL", "headers": {"Location": {"schema": {"type": "string", "format": "uri"}}}}},
)
def download_file(
    source: BuildSource, ref: Annotated[str, Path(min_length=1)], arch: Architecture,
    name: FileName,
    query: Annotated[EmptyQuery, Query()], manager: Manager,
) -> RedirectResponse:
    build = (
        require_latest_build(manager, source, LatestBuildQuery(arch=arch, file=name))
        if ref == "latest" else require_build(manager, source, ref)
    )
    artifact = manager.get_file(build, arch, name)
    if artifact is None:
        raise APIError(404, "file_not_found", f"No {arch.value} file named {name} in {source.value} build {ref}")
    return RedirectResponse(manager.get_presigned_url(artifact), headers={"Cache-Control": "no-store"})


@build_router.get("/{source}/{ref:path}", response_model=BuildDetail, name="get_build", summary="Inspect a mirrored build")
def get_build(
    request: Request, source: BuildSource, ref: Annotated[str, Path(min_length=1)],
    filters: Annotated[BuildDetailQuery, Query()], manager: Manager,
) -> BuildDetail:
    return BuildDetail.from_build(
        require_build(manager, source, ref), lambda route, **params: str(request.url_for(route, **params)), filters.arch,
    )
