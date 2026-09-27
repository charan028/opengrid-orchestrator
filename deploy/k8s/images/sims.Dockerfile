# deploy/k8s/images/sims.Dockerfile -- the integration simulators (package ogsim: market, fleet, scada, control,
# customer), an independent product that shares only interfaces/ with the orchestrator.
# Build context: the repository root.
#   docker build -f deploy/k8s/images/sims.Dockerfile -t <registry>/opengrid-sims:<tag> .
# Layout mirrors production: release tree at /opt/opengrid/current, dependencies in /opt/ogsim/venv.

ARG PYTHON_IMAGE=docker.io/library/python:3.13.15-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b

FROM ${PYTHON_IMAGE} AS deps
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY deploy/k8s/images/constraints-sims.txt /tmp/constraints.txt
COPY integration-sims/pyproject.toml integration-sims/README.md /tmp/sims/
COPY integration-sims/src /tmp/sims/src
RUN python -m venv /opt/ogsim/venv \
 && /opt/ogsim/venv/bin/pip install --prefer-binary -c /tmp/constraints.txt /tmp/sims \
 && /opt/ogsim/venv/bin/pip uninstall -y ogsim

FROM ${PYTHON_IMAGE}
ARG OG_REVISION=unknown
LABEL org.opencontainers.image.title="opengrid-sims" \
      org.opencontainers.image.description="OpenGrid integration simulators (ogsim)" \
      org.opencontainers.image.revision="${OG_REVISION}" \
      org.opencontainers.image.source="https://git.tocy-net.net/Tocy-Net/opengrid-orchestrator"
RUN groupadd --system --gid 10001 opengrid \
 && useradd --system --uid 10001 --gid opengrid --home-dir /opt/opengrid --shell /usr/sbin/nologin opengrid
COPY --from=deps /opt/ogsim/venv /opt/ogsim/venv
COPY interfaces /opt/opengrid/current/interfaces
COPY integration-sims/src /opt/opengrid/current/integration-sims/src
COPY integration-sims/config /opt/opengrid/current/integration-sims/config
COPY integration-sims/scenarios /opt/opengrid/current/integration-sims/scenarios
COPY deploy/scripts/gen_sim_overrides.py /opt/opengrid/current/deploy/scripts/gen_sim_overrides.py
COPY --chmod=0755 deploy/k8s/images/og-entrypoint.sh /usr/local/bin/og-entrypoint
RUN /opt/ogsim/venv/bin/python -m compileall -q /opt/opengrid/current/integration-sims/src \
 && echo "${OG_REVISION}" > /opt/opengrid/current/REVISION
ENV PATH=/opt/ogsim/venv/bin:$PATH \
    PYTHONPATH=/opt/opengrid/current/integration-sims/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /opt/opengrid/current/integration-sims
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/og-entrypoint"]
CMD ["sim-market"]
