# Background worker image - no web server, no exposed port by default.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC

WORKDIR /app

# Dependencies first so code edits do not invalidate the wheel layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Railway mounts the volume here; the store falls back to /tmp if it is absent.
ENV DATA_DIR=/data
RUN mkdir -p /data

# Runs as root deliberately: Railway volumes are mounted root-owned, and a
# non-root process would silently lose the dedupe database to the /tmp
# fallback. See README ("Running as a non-root user") to change this.
CMD ["python", "-m", "app.main"]
