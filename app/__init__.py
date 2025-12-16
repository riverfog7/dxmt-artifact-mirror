import asyncio
import logging
from contextlib import asynccontextmanager

import dotenv
from fastapi import FastAPI
from sqlmodel import SQLModel

from .artifact_manager import DXMTArtifactManager
from .github import GitHubAPIClient
from .router import router, artifact_router, build_router
from .syncer import ArtifactSyncer
from .utils import engine, get_bucket_name, get_endpoint_url

dotenv.load_dotenv(dotenv.find_dotenv())

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables
    SQLModel.metadata.create_all(engine)

    # Start syncer in background
    github_client = GitHubAPIClient()
    syncer = ArtifactSyncer(github_client, engine, get_bucket_name())

    task = asyncio.create_task(syncer.sync_loop())

    yield

    # Cleanup
    task.cancel()

app = FastAPI(lifespan=lifespan)

app.include_router(router)
app.include_router(artifact_router)
app.include_router(build_router)
