# Python 3.11.17 / Debian trixie slim; keep the multi-platform digest immutable.
FROM python:3.11.17-slim-trixie@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce
# Do not add USER or a recursive-chown entrypoint without a one-time host
# migration. The production ./data bind mount contains existing root-owned
# SQLite/spool files, so a fixed non-root UID cannot write them. Compose masks
# data/releases read-only inside application containers, but a host-side
# recursive chown would still weaken that registry. Migrate only an allowlisted
# runtime path before USER is enabled.
WORKDIR /app
COPY requirements-prod.txt .
# Install wheels only: production does not build sdists or need packaging tools.
# Remove the tools AND ensurepip's bundled archives in this same layer. Upgrading
# top-level wheel alone does not replace setuptools/pip's private vendor copies.
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN python -m pip install --no-cache-dir --only-binary=:all: --upgrade "pip==26.2.0" \
      -i "$PIP_INDEX_URL" \
 && python -m pip install --no-cache-dir --only-binary=:all: -r requirements-prod.txt \
      -i "$PIP_INDEX_URL" \
 && python -m pip check \
 && python -m pip uninstall --yes setuptools wheel pip \
 && python -c "import pathlib, shutil, sysconfig; shutil.rmtree(pathlib.Path(sysconfig.get_path('stdlib')) / 'ensurepip')"
ARG AI_FILTER_BUILD_VERSION=development
ARG AI_FILTER_BUILD_GIT_SHA=unknown
ARG AI_FILTER_BUILD_MANIFEST_SHA=unknown
ARG AI_FILTER_BUILD_CONFIG_SHA=unknown
ARG AI_FILTER_BUILD_SOURCE_ID=unknown
ARG AI_FILTER_PRIVATE_RELEASE_SHA=none
ARG AI_FILTER_RUNTIME_CONFIG_SHA=unknown
ARG AI_FILTER_MAPPING_MIGRATION_GENERATION=event-mapping-store-v5-provider-attribution-v1
ENV AI_FILTER_VERSION=${AI_FILTER_BUILD_VERSION}
ENV AI_FILTER_REVISION=${AI_FILTER_BUILD_GIT_SHA}
LABEL org.opencontainers.image.version=${AI_FILTER_BUILD_VERSION}
LABEL org.opencontainers.image.revision=${AI_FILTER_BUILD_GIT_SHA}
LABEL io.ai-filter.compose-manifest.sha256=${AI_FILTER_BUILD_MANIFEST_SHA}
LABEL io.ai-filter.config.sha256=${AI_FILTER_BUILD_CONFIG_SHA}
LABEL io.ai-filter.source.identity=${AI_FILTER_BUILD_SOURCE_ID}
LABEL io.ai-filter.private-release.sha256=${AI_FILTER_PRIVATE_RELEASE_SHA}
LABEL io.ai-filter.runtime-config.sha256=${AI_FILTER_RUNTIME_CONFIG_SHA}
LABEL io.ai-filter.event-mapping-generation=${AI_FILTER_MAPPING_MIGRATION_GENERATION}
COPY config.yaml .
COPY app.py .
COPY logging_bootstrap.py .
COPY sse_engine.py .
COPY worker_main.py .
COPY start.sh .
COPY gunicorn.conf.py .
COPY scoring_provenance.py .
COPY scoring_explanation.py .
COPY core/ ./core/
COPY db/ ./db/
COPY utils/ ./utils/
COPY routes/ ./routes/
COPY services/ ./services/
COPY templates/ ./templates/
COPY workers/ ./workers/
COPY modules/ ./modules/
COPY scripts/run_event_enrichment.py ./scripts/run_event_enrichment.py
COPY scripts/run_provenance_shadow.py ./scripts/run_provenance_shadow.py
COPY scripts/export_score_gold_candidates.py ./scripts/export_score_gold_candidates.py
COPY scripts/import_score_gold_labels.py ./scripts/import_score_gold_labels.py
COPY scripts/score_gold_metrics.py ./scripts/score_gold_metrics.py
COPY scripts/run_controlled_startup.py ./scripts/run_controlled_startup.py
# Runtime admission verifies the same sealed host-controller source bytes.
# These are evidence inputs only: no Docker socket, host namespace or authority
# is added to the application container.
COPY scripts/production_sender_driver.py ./scripts/production_sender_driver.py
COPY scripts/sender_drain_inventory.py ./scripts/sender_drain_inventory.py
COPY scripts/sender_host_loopback_pg.py ./scripts/sender_host_loopback_pg.py
COPY scripts/observe_handoff_capacity.py ./scripts/observe_handoff_capacity.py
COPY scripts/production_admission_supervisor.py ./scripts/production_admission_supervisor.py
COPY scripts/production_admission_watchdog.py ./scripts/production_admission_watchdog.py
COPY scripts/sender_namespace_http.py ./scripts/sender_namespace_http.py
COPY scripts/retained_gateway_configuration.py ./scripts/retained_gateway_configuration.py
COPY scripts/retained_gateway_custody.py ./scripts/retained_gateway_custody.py
RUN chmod +x /app/start.sh && python -m py_compile app.py sse_engine.py worker_main.py gunicorn.conf.py scoring_provenance.py scoring_explanation.py core/*.py db/*.py utils/*.py routes/*.py services/*.py workers/*.py modules/*.py scripts/run_event_enrichment.py scripts/run_provenance_shadow.py scripts/export_score_gold_candidates.py scripts/import_score_gold_labels.py scripts/score_gold_metrics.py \
    && python -m py_compile scripts/run_controlled_startup.py \
    && python -c "import scoring_explanation; import scripts.retained_gateway_configuration; import scripts.retained_gateway_custody"
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:5000/health', timeout=3).read()"
# gunicorn 生产服务器（worker 进程用 command 覆盖为 worker_main.py）
CMD ["/app/start.sh"]
