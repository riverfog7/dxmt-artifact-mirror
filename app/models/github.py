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
