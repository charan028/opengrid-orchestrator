# dev/docker/orchestrator.Dockerfile -- image for the `migrate` one-shot service and the
# optional `orchestrator` compose profile (BUILD.md WP D0 items 1/5). Build context is the
# repo root (see dev/docker-compose.yml's `build.context: ..`) so this can COPY the whole
# `orchestrator/` tree, including `migrations/`, which `opengrid.platform.db` locates relative
# to its own installed path (parents[3] of db.py) -- installing editable (`pip install -e`)
# keeps that path resolution identical to a real checkout instead of a built wheel's
# site-packages layout.
FROM python:3.13-slim

WORKDIR /app

# Build deps for any wheel-less sdists (e.g. highspy on less common platforms); removed from
# the final layer is not attempted here since this is a dev-only image, not a production
# artifact -- BUILD.md's lean-image rules apply to deploy/, not dev/.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY orchestrator/ orchestrator/

RUN pip install --no-cache-dir -e ./orchestrator
# orchestrator/pyproject.toml declares fastapi>=0.110 but not python-multipart. FastAPI needs it
# eagerly at *route-registration* time (not just when a Form is actually posted) for any
# `Form(...)` parameter -- opengrid/ui/routes/health.py's `POST /alerts/ack` has one -- so og-api
# fails at import with "RuntimeError: Form data requires python-multipart to be installed"
# before it ever binds its port. Product-code gap (needs an owner to add it to
# orchestrator/pyproject.toml's dependencies); installed here as a dev-stack workaround so the
# orchestrator profile's image matches what og-api's own code actually needs to import cleanly.
RUN pip install --no-cache-dir python-multipart

# dev/ itself is bind-mounted at runtime (docker-compose.yml), not copied, so editing
# dev/config/*.toml or dev/config/*.yaml never requires an image rebuild.

ENTRYPOINT []
