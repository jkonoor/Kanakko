# One image, two commands: uvicorn for `web`, cron for the sidecar
# (DECISIONS §8). Anything the sidecar needs must therefore be in here.
FROM python:3.12-slim

# cron is for the sidecar only; `web` never invokes it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# uv.lock, not pyproject's floors — the deployed versions are the tested ones.
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
COPY kanakko ./kanakko
COPY migrations ./migrations
RUN uv sync --frozen --no-dev

# uv installs the project editable, so kanakko/migrate.py resolves MIGRATIONS
# to /app/migrations. That is why migrations/ is COPYed rather than packaged
# into the wheel (REVIEWS.md, finding 1 on 3f33abf).
ENV PATH="/app/.venv/bin:$PATH"
