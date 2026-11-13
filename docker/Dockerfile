# Runtime image for API/MCP, worker, migration and admin/recovery tools (one codebase, separate processes).
# Contains no secrets and no test code; configuration arrives via environment and mounted secret files.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates python3 python3-venv python3-psycopg2 \
        postgresql-client-16 gnupg \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv --system-site-packages /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -q -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/app/src

WORKDIR /app
COPY migrations ./migrations
COPY src ./src
RUN useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin memory \
 && mkdir -p /ledger /backups && chown memory /ledger /backups
USER memory
EXPOSE 8080
CMD ["python3", "-m", "memory_server.app", "--host", "0.0.0.0", "--port", "8080"]
