# One image, two commands: uvicorn for `web`, cron for the sidecar
# (DECISIONS §8). Anything the sidecar needs must therefore be in here.
FROM python:3.12-slim

# cron is for the sidecar only; `web` never invokes it. tzdata is not optional:
# glibc resolves an unknown TZ to UTC silently and exit 0, so without the zone
# files `TZ: Asia/Kolkata` is a no-op and the 21:00 summary fires at 02:30 IST.
RUN apt-get update \
    && apt-get install -y --no-install-recommends cron tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# uv.lock, not pyproject's floors — the deployed versions are the tested ones.
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
COPY kanakko ./kanakko
COPY migrations ./migrations
COPY cron ./cron
RUN uv sync --frozen --no-dev

# The sidecar's crontab (DECISIONS §12). /etc/cron.d entries must be root-owned
# and not group/world-writable or cron silently ignores the file; 0644 satisfies
# both. The web service never reads this — it runs the uvicorn CMD below.
RUN cp cron/kanakko.crontab /etc/cron.d/kanakko \
    && chmod 0644 /etc/cron.d/kanakko \
    && chmod 0755 cron/entrypoint.sh

# uv installs the project editable, so kanakko/migrate.py resolves MIGRATIONS
# to /app/migrations. That is why migrations/ is COPYed rather than packaged
# into the wheel (REVIEWS.md, finding 1 on 3f33abf).
ENV PATH="/app/.venv/bin:$PATH"

# The image must be runnable with no external command. Dokploy's per-service
# `command` field is an argv array split on whitespace, so a shell-form command
# set there arrives as ["sh","-c","\"python","-m",...] and dies with
# "Unterminated quoted string". Exec-form CMD here is immune, and the sidecar
# still overrides it with the single-token `cron -f`, which survives splitting.
CMD ["sh", "-c", "python -m kanakko.migrate && exec uvicorn kanakko.app:app --host 0.0.0.0 --port 8000"]
