import os
from typing import Optional

import requests
from pathlib import Path

from .models.github import (
    GitHubActionRunsResponse,
    GitHubActionArtifactsResponse,
)


class GitHubAPIClient:
    BASE_URL = "https://api.github.com"
    HEADERS = {
        "Accept": "Accept: application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    def __init__(self, token: Optional[str] = None):
        if token is None:
            token = os.getenv("GITHUB_TOKEN")

        self.headers = self.HEADERS.copy()
        if token == "":
            raise ValueError("GitHub token must be provided either as an argument or via the GITHUB_TOKEN environment variable.")
        self.headers["Authorization"] = f"Bearer {token}"


    def get_action_runs(self, owner: str, repo: str, per_page: int = 30, page: int = 1):
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/actions/runs"
        params = {
            "per_page": per_page,
            "page": page
        }
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()

        return GitHubActionRunsResponse.model_validate_json(response.text)


    def get_run_artifacts(self, owner: str, repo: str, run_id: int, per_page: int = 30, page: int = 1):
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts"
        params = {
            "per_page": per_page,
            "page": page
        }
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()

        return GitHubActionArtifactsResponse.model_validate_json(response.text)


    def download_artifact(self, dest_path: Path, owner: str, repo: str, artifact_id: int):
        url = f"{self.BASE_URL}/repos/{owner}/{repo}/actions/artifacts/{artifact_id}/zip"
        response = requests.get(url, headers=self.headers, stream=True)
        response.raise_for_status()

        with open(dest_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

        return dest_path
