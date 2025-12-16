from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import List, Optional


class GitHubActionRun(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    name: str
    head_branch: str
    head_sha: str
    display_title: str
    status: str
    conclusion: Optional[str]
    updated_at: Optional[datetime]
    created_at: datetime
    run_started_at: Optional[datetime]
    artifacts_url: str
    workflow_id: int
    path: str


class GitHubActionRunsResponse(BaseModel):
    total_count: int
    workflow_runs: List[GitHubActionRun]


class GitHubActionArtifact(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    name: str
    digest: str
    expired: bool


class GitHubActionArtifactsResponse(BaseModel):
    total_count: int
    artifacts: List[GitHubActionArtifact]


class GitHubReleaseAsset(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    name: str
    content_type: str
    size: int
    download_count: int
    created_at: datetime
    updated_at: datetime
    browser_download_url: str


class GitHubRelease(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    tag_name: str
    target_commitish: str
    name: str
    draft: bool
    prerelease: bool
    created_at: datetime
    published_at: datetime
    assets: List[GitHubReleaseAsset]
    body: str
