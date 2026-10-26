# DXMT Artifact Mirror

This service mirrors artifacts from the [DXMT](https://github.com/3Shain/dxmt) project, providing a reliable and fast way to download both CI builds and official releases. It automatically syncs with GitHub to ensure the latest artifacts are always available.

## Features

- **Automatic Sync**: Periodically checks for new GitHub Actions runs and Releases.
- **Artifact Mirroring**: Downloads and stores artifacts in an S3-compatible storage.
- **Wow64 Support**: Distinguishes between 64-bit and 32-bit (Wow64) artifacts.
- **ARM64EC Support**: Mirrors ARM64EC DLLs and the arm64 Unix library separately from x64 and Wow64 files.
- **Fast Downloads**: Serves artifacts via presigned S3 URLs for optimal performance.
- **API Access**: Provides a REST API to list builds, artifacts, and generate download links.

## CI Sources

CI mirroring selects only unexpired Clang release artifacts:

- `clang-release-x86_64-windows-cross`: x64 DLLs (including `d3d12.dll` when provided) and the x64 Unix library.
- `clang-release-x86-windows-cross`: i386 DLLs for Wow64.
- `clang-release-arm64ec-windows-cross`: ARM64EC DLLs and the arm64 Unix library.

Combined, GCC, debugoptimized, and native macOS CI archives are not selected. Missing or expired Clang sources are skipped. Tagged releases continue to use their published upstream tarballs.

On each startup, a backfill revisits existing CI builds, newest first, five builds per sync cycle alongside normal polling. It checks GitHub artifact expiry directly, including for builds older than the normal 100-day new-run window. Available Clang files replace existing GCC files at the same URLs; ARM64EC files and `d3d12.dll` are added when supplied. Missing or expired sources leave existing files intact.

Source artifact IDs are saved after all uploads for a build succeed. Repeated backfills, including after restarting, skip downloads for completed sources and update existing file records without duplication. Failed imports are rolled back and queued for a later cycle. Historical builds without `d3d12.dll` are not repeatedly downloaded just because that file is absent. The backfill stops inspecting old runs once its queue is complete and starts a new pass on the next service startup.

## API Documentation

### Base URL

The API is served at the deployed service root, for example `http://localhost:8000`. Interactive documentation is at `/docs`; the complete Pydantic request and response schemas are at `/openapi.json`.

The public API uses `source=ci|release` and `arch=x86|x86_64|arm64ec`. `x86` selects i386 DLLs for Wow64. `x86_64` and `arm64ec` each include their Windows DLLs and corresponding Unix library. Omitting `arch` includes all available architectures.

### Discover and Inspect Builds

| Request | Result |
|---|---|
| `GET /builds` | CI builds and releases, newest first |
| `GET /builds?source=ci&arch=arm64ec` | CI builds with mirrored ARM64EC files |
| `GET /builds?commit=<full-sha>` | Every CI run for that commit, newest first |
| `GET /builds/ci/{run_id}` | CI metadata and files |
| `GET /builds/release/{tag}` | Release metadata and files |
| `GET /builds/ci/{run_id}?arch=x86_64` | Build metadata with only x64 files in `files` |
| `GET /builds/ci/latest?arch=x86_64&file=d3d12.dll` | Newest CI build with x64 `d3d12.dll` |
| `GET /builds/release/latest?arch=x86_64` | Newest release with x64 files |
| `GET /health` | `{"status":"ok"}` |

`GET /builds` accepts `source`, `arch`, `commit`, `page` (default 1, minimum 1), and `per_page` (default 20, range 1–100). `commit` is a full 40-character SHA and applies only to CI builds. Unknown query parameters are rejected. Ordering uses creation time descending, source, CI run ID descending, then reference descending so ties are deterministic.

List responses contain `items`, `page`, `per_page`, `total`, and `has_more`. Both CI and release summaries use the same fields: `source`, `ref`, `url`, `created_at` (UTC), `commit_sha`, `description`, `architectures`, and `file_count`. `ref` is the run ID as a string or the release tag; `url` links to its build details. Release `commit_sha` and `description` are `null`. Counts and available architectures come from mirrored file records, including files retained after their upstream artifacts expire.

Build details add a `files` array with `name`, `arch`, and `download_url`. Follow these links instead of constructing paths. An `arch` filter narrows this array; metadata still describes the entire build. Existing builds with no files for that architecture return an empty array. For example, a file entry is:

```json
{
  "name": "d3d12.dll",
  "arch": "x86_64",
  "download_url": "http://localhost:8000/builds/ci/39904585849/files/x86_64/d3d12.dll"
}
```

`latest` is a reserved build reference. Latest lookup accepts optional `arch` and `file` filters and selects the newest build containing a matching file record, skipping builds without files. With both filters, the file must exist in that architecture. These filters select the build; `arch` also filters the returned `files` array. Latest responses include canonical build and file links with the resolved run ID or tag and use `Cache-Control: no-store`. CI lookup covers all mirrored runs, including pull requests; it does not imply a main-branch build.

### Download Files

| Request | Result |
|---|---|
| `GET /builds/ci/{run_id}/files/{arch}/{name}` | Download a CI file |
| `GET /builds/release/{tag}/files/{arch}/{name}` | Download a release file |
| `GET /builds/ci/latest/files/{arch}/{name}` | Download the newest matching CI file |
| `GET /builds/release/latest/files/{arch}/{name}` | Download the newest matching release file |

Downloads return HTTP 307 with a temporary signed S3 URL in `Location`. Redirects are marked `Cache-Control: no-store`. For example:

```sh
curl -fL -o d3d12.dll http://localhost:8000/builds/ci/39904585849/files/x86_64/d3d12.dll
curl -fL -o d3d12.dll http://localhost:8000/builds/ci/latest/files/x86_64/d3d12.dll
```

Metadata links are stable API URLs. Signed storage URLs are generated only on download; metadata requests do not access S3. Returned links respect the deployment root path and encode tags and file names.

### Errors

Invalid sources, architectures, filters, or pagination return `422`. A missing build or file returns `404`. Empty searches return `200` with `items: []`. Errors share the shape:

```json
{"error":{"code":"build_not_found","message":"No mirrored ci build found for 123","details":[]}}
```

Validation errors use `code: "invalid_request"`; `details` contains the failing location, message, and validation type. Internal errors return a generic `500` response without exposing server details. Legacy `/artifacts/*`, `/builds/list`, download routes, and boolean architecture query parameters have been removed.

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

At startup, existing databases are automatically migrated before syncing. The migration adds `is_arm64ec` to CI artifact records, defaulting existing rows to `false`, and is safe to repeat.

## Tests

Run the database, API, import, and backfill checks with `uv run python -m unittest discover -s tests -v`.

