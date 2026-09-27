# deploy/k8s/images/orchestrator.Dockerfile -- one image for every og-* service, the migrations/seed/check Jobs
# and the lifecycle CronJob; `og-entrypoint <service>` selects what runs (deploy/k8s/README.md).
# Build context: the repository root.
#   docker build -f deploy/k8s/images/orchestrator.Dockerfile -t <registry>/opengrid-orchestrator:<tag> .
# Layout mirrors production: the release tree at /opt/opengrid/current (PYTHONPATH=orchestrator/src, as the
# systemd units set it) and the dependencies in /opt/opengrid/venv, so deploy/scripts run unchanged.

ARG PYTHON_IMAGE=docker.io/library/python:3.13.15-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b

FROM ${PYTHON_IMAGE} AS deps
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY deploy/k8s/images/constraints-orchestrator.txt /tmp/constraints.txt
COPY orchestrator/pyproject.toml orchestrator/README.md /tmp/orchestrator/
COPY orchestrator/src /tmp/orchestrator/src
# Dependencies only: the code itself runs from the release tree (MIGRATIONS_DIR and interfaces/ resolve relative
# to it), exactly like production's venv.
RUN python -m venv /opt/opengrid/venv \
 && /opt/opengrid/venv/bin/pip install --prefer-binary -c /tmp/constraints.txt /tmp/orchestrator \
 && /opt/opengrid/venv/bin/pip uninstall -y opengrid

FROM ${PYTHON_IMAGE}
ARG OG_REVISION=unknown
LABEL org.opencontainers.image.title="opengrid-orchestrator" \
      org.opencontainers.image.description="OpenGrid orchestrator: og-* services, migrations, seeds, lifecycle" \
      org.opencontainers.image.revision="${OG_REVISION}" \
      org.opencontainers.image.source="https://git.tocy-net.net/Tocy-Net/opengrid-orchestrator"
# psql/pg_isready/pg_dump for create_schema.sh and the SQL seeds; the client matches the Postgres 17 server.
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-17 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 10001 opengrid \
 && useradd --system --uid 10001 --gid opengrid --home-dir /opt/opengrid --shell /usr/sbin/nologin opengrid
COPY --from=deps /opt/opengrid/venv /opt/opengrid/venv
COPY interfaces /opt/opengrid/current/interfaces
COPY orchestrator/src /opt/opengrid/current/orchestrator/src
COPY orchestrator/migrations /opt/opengrid/current/orchestrator/migrations
COPY orchestrator/schema /opt/opengrid/current/orchestrator/schema
COPY orchestrator/config /opt/opengrid/current/orchestrator/config
COPY orchestrator/tools /opt/opengrid/current/orchestrator/tools
COPY dev/seed /opt/opengrid/current/dev/seed
COPY dev/scripts /opt/opengrid/current/dev/scripts
COPY dev/secrets.example /opt/opengrid/current/dev/secrets.example
COPY deploy/scripts /opt/opengrid/current/deploy/scripts
COPY integration-sims/config /opt/opengrid/current/integration-sims/config
COPY deploy/k8s/images/k8s_support.py /opt/opengrid/k8s/k8s_support.py
COPY --chmod=0755 deploy/k8s/images/og-entrypoint.sh /usr/local/bin/og-entrypoint
RUN /opt/opengrid/venv/bin/python -m compileall -q /opt/opengrid/current/orchestrator/src /opt/opengrid/k8s \
 && echo "${OG_REVISION}" > /opt/opengrid/current/REVISION
ENV PATH=/opt/opengrid/venv/bin:$PATH \
    PYTHONPATH=/opt/opengrid/current/orchestrator/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OG_CONFIG=/run/og/orchestrator.toml
WORKDIR /opt/opengrid/current/orchestrator
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/og-entrypoint"]
CMD ["api"]
