from typing import Optional

from fastapi import Depends, HTTPException
from fastapi.routing import APIRouter
from fastapi.responses import RedirectResponse
from sqlmodel import Session

from .utils import get_db, get_bucket_name, get_endpoint_url
from .artifact_manager import DXMTArtifactManager

router = APIRouter()
artifact_router = APIRouter(prefix="/artifacts")
build_router = APIRouter(prefix="/builds")

def get_artifact_manager(session: Session = Depends(get_db)):
    return DXMTArtifactManager(session, bucket_name=get_bucket_name(), endpoint_url=get_endpoint_url())

@router.get("/health")
async def health_check():
    return {"status": "ok"}


@artifact_router.get("/list")
async def list_artifacts(
    tag: Optional[str] = None,
    id: Optional[int] = None,
    commit_sha: Optional[str] = None,
    wow64: bool = False,
    manager: DXMTArtifactManager = Depends(get_artifact_manager)
):
    if not tag and not id and not commit_sha:
        return {"error": "At least one of tag, id, or commit_sha must be provided"}
    if tag and (id or commit_sha):
        return {"error": "tag cannot be combined with id or commit_sha"}
    if id and commit_sha:
        return {"error": "id cannot be combined with commit_sha"}

    artifacts = manager.list_artifacts(tag=tag, id=id, commit_sha=commit_sha, wow64=wow64)
    return {"artifacts": artifacts}


@build_router.get("/list")
async def list_builds(
    page: int = 1,
    page_size: int = 10,
    manager: DXMTArtifactManager = Depends(get_artifact_manager)
):
    builds = manager.list_builds(page=page, page_size=page_size)
    return {"builds": builds}


@build_router.get("/download/{github_run_id}/artifact/{artifact_name}")
async def download_build_artifact(
    github_run_id: int,
    artifact_name: str,
    wow64: bool = False,
    manager: DXMTArtifactManager = Depends(get_artifact_manager)
):
    # Find the artifact
    artifacts = manager.list_artifacts(id=github_run_id, wow64=wow64)
    target_artifact = next((a for a in artifacts if a.name == artifact_name), None)

    if not target_artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    download_url = manager.get_presigned_url(target_artifact)
    return RedirectResponse(url=download_url)

@artifact_router.get("/download/{tag}/artifact/{artifact_name}")
async def download_release_artifact(
    tag: str,
    artifact_name: str,
    wow64: bool = False,
    manager: DXMTArtifactManager = Depends(get_artifact_manager)
):
    # Find the artifact
    artifacts = manager.list_artifacts(tag=tag, wow64=wow64)
    target_artifact = next((a for a in artifacts if a.name == artifact_name), None)

    if not target_artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    download_url = manager.get_presigned_url(target_artifact)
    return RedirectResponse(url=download_url)
