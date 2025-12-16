import os

from sqlmodel import create_engine, Session

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./dxmt_mirror.db")

engine = create_engine(DATABASE_URL, echo=True)

def get_db():
    with Session(engine) as session:
        yield session

def get_bucket_name() -> str:
    return os.environ.get("S3_BUCKET_NAME", "dxmt-artifacts")

def get_endpoint_url() -> str:
    return os.environ.get("S3_ENDPOINT_URL", None)

