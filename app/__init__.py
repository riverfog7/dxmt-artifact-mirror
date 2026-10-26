import asyncio
import logging
from contextlib import asynccontextmanager

import dotenv
from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from .errors import install_error_handlers
from .github import GitHubAPIClient
from .router import router, build_router
from .syncer import ArtifactSyncer
from .utils import engine, get_bucket_name, get_endpoint_url, init_db

dotenv.load_dotenv(dotenv.find_dotenv())

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables and migrate existing databases before starting the syncer.
    init_db()

    # Start syncer in background
    github_client = GitHubAPIClient()
    syncer = ArtifactSyncer(github_client, engine, get_bucket_name())

    task = asyncio.create_task(syncer.sync_loop())

    yield

    # Cleanup
    task.cancel()

app = FastAPI(title="DXMT Artifact Mirror", description="Discover mirrored CI builds, releases, and runtime files.", lifespan=lifespan)
install_error_handlers(app)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins - adjust if you want to restrict
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

app.include_router(router)
app.include_router(build_router)
