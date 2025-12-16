# DXMT Artifact Mirror

This service mirrors artifacts from the [DXMT](https://github.com/3Shain/dxmt) project, providing a reliable and fast way to download both CI builds and official releases. It automatically syncs with GitHub to ensure the latest artifacts are always available.

## Features

- **Automatic Sync**: Periodically checks for new GitHub Actions runs and Releases.
- **Artifact Mirroring**: Downloads and stores artifacts in an S3-compatible storage.
- **Wow64 Support**: Distinguishes between 64-bit and 32-bit (Wow64) artifacts.
- **Fast Downloads**: Serves artifacts via presigned S3 URLs for optimal performance.
- **API Access**: Provides a REST API to list builds, artifacts, and generate download links.

## API Documentation

### Base URL

The API is served at the root of the deployed service. For example: `http://localhost:8000`.

### Health Check

Check if the service is running.

- **Endpoint**: `GET /health`
- **Response**: `{"status": "ok"}`

### Builds

#### List Builds

Retrieve a paginated list of all builds (both CI builds and Releases), sorted by creation date (newest first).

- **Endpoint**: `GET /builds/list`
- **Parameters**:
  - `page` (int, optional): Page number (default: 1).
  - `page_size` (int, optional): Number of items per page (default: 10).
- **Response**: A JSON object containing a list of builds. Each build object includes details like `id` (run ID or tag), `type` ("builtin" or "release"), `created_at`, `artifact_count`, and `has_wow64`.

### Artifacts

#### List Artifacts

List specific artifacts for a given build. You must provide exactly one of `tag`, `id`, or `commit_sha`.

- **Endpoint**: `GET /artifacts/list`
- **Parameters**:
  - `tag` (string, optional): The release tag (e.g., `v1.0.0`). Use this for Release builds.
  - `id` (int, optional): The GitHub Run ID. Use this for CI builds.
  - `commit_sha` (string, optional): The commit SHA. Use this for CI builds.
  - `wow64` (boolean, optional): Set to `true` to list 32-bit artifacts. Defaults to `false` (64-bit).
- **Response**: A JSON object containing a list of artifact details.

#### Download Build Artifact (CI)

Download a specific artifact from a CI build. This endpoint redirects to a temporary, presigned S3 URL.

- **Endpoint**: `GET /builds/download/{github_run_id}/artifact/{artifact_name}`
- **Path Parameters**:
  - `github_run_id` (int): The GitHub Action Run ID.
  - `artifact_name` (string): The name of the file to download (e.g., `d3d11.dll`).
- **Query Parameters**:
  - `wow64` (boolean, optional): Set to `true` if downloading a 32-bit artifact. Defaults to `false`.

#### Download Release Artifact

Download a specific artifact from a Release. This endpoint redirects to a temporary, presigned S3 URL.

- **Endpoint**: `GET /artifacts/download/{tag}/artifact/{artifact_name}`
- **Path Parameters**:
  - `tag` (string): The release tag (e.g., `v0.3`).
  - `artifact_name` (string): The name of the file to download.
- **Query Parameters**:
  - `wow64` (boolean, optional): Set to `true` if downloading a 32-bit artifact. Defaults to `false`.

## Deployment

### Docker Compose

You can deploy the service using Docker Compose.

1.  **Create a `.env` file** with your configuration:

    ```dotenv
    AWS_ACCESS_KEY_ID=your_access_key
    AWS_SECRET_ACCESS_KEY=your_secret_key
    S3_ENDPOINT_URL=https://s3.yourdomain.com
    S3_BUCKET_NAME=dxmt-artifacts
    DATABASE_URL=sqlite:////data/dxmt_mirror.db
    GITHUB_TOKEN=your_github_token
    ```

2.  **Run the service**:

    ```bash
    docker compose up -d
    ```

The service will be available at `http://localhost:8000`. Data will be persisted in the `./data` directory.

