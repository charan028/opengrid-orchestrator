# dev/docker/sims.Dockerfile -- shared image for the 4 integration-sims services
# (sim-market, sim-fleet, sim-scada, sim-control). Build context is the repo root (see
# dev/docker-compose.yml's `build.context: ..`).
FROM python:3.13-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY integration-sims/ integration-sims/

RUN pip install --no-cache-dir -e ./integration-sims

# dev/ (dev/config/*.dev.yaml, dev/keys/*.pub) is bind-mounted at runtime, not copied.

ENTRYPOINT []
