FROM python:3.13-slim-bookworm

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Set working directory
WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-install-project

# Copy application code
COPY app ./app

# Set environment variables with default values (can be overridden at runtime)
ENV AWS_ACCESS_KEY_ID=""
ENV AWS_SECRET_ACCESS_KEY=""
ENV S3_ENDPOINT_URL=""
ENV DATABASE_URL="sqlite:////data/dxmt_mirror.db"
ENV GITHUB_TOKEN=""
ENV PATH="/app/.venv/bin:$PATH"

# Expose port
EXPOSE 8000

# Run the application
CMD ["uv", "run", "fastapi", "run", "app", "--port", "8000", "--host", "0.0.0.0"]
