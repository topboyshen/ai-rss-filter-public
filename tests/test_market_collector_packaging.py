from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_market_collector_compose_profile_is_opt_in_and_immutable():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    collector = services["market-close-collector"]

    assert collector["profiles"] == ["cron"]
    assert collector["image"] == services["ai-filter-api"]["image"]
    assert collector["image"] == services["ai-filter-worker"]["image"]
    assert collector["command"] == "python workers/market_close_collector.py"
    assert "./data:/app/data" in collector["volumes"]
    assert "./config.yaml:/app/config.yaml:ro" not in collector["volumes"]
    assert collector["restart"] == "no"
    assert collector["healthcheck"] == {"disable": True}
    assert "com.centurylinklabs.watchtower.enable=false" in collector["labels"]


def test_security_master_sync_is_secretless_opt_in_and_dry_run_by_default():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    sync = services["security-master-sync"]

    assert sync["profiles"] == ["security-master"]
    assert sync["image"] == services["ai-filter-api"]["image"]
    assert sync["command"] == "python workers/security_master_collector.py"
    assert "--apply" not in sync["command"]
    assert "env_file" not in sync
    assert "secrets" not in sync
    assert "./data:/app/data" in sync["volumes"]
    assert "./config.yaml:/app/config.yaml:ro" not in sync["volumes"]
    assert sync["read_only"] is True
    assert sync["cap_drop"] == ["ALL"]
    assert any(str(item).startswith("/tmp:") for item in sync["tmpfs"])
    assert sync["environment"]["ALL_PROXY"] == ""
    assert sync["environment"]["all_proxy"] == ""
    assert sync["healthcheck"] == {"disable": True}
    assert sync["restart"] == "no"
    assert "com.centurylinklabs.watchtower.enable=false" in sync["labels"]


def test_redis_retains_aof_volume_and_bounded_memory_policy():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    redis = compose["services"]["redis"]
    command = [str(value) for value in redis["command"]]
    expected_image = (
        "redis:7-alpine@sha256:"
        "6ab0b6e7381779332f97b8ca76193e45b0756f38d4c0dcda72dbb3c32061ab99"
    )
    assert redis["image"] == expected_image
    assert redis["image"].startswith("redis:7-alpine@sha256:")
    assert redis["image"] != "redis:7-alpine"

    assert command[:3] == ["redis-server", "--appendonly", "yes"]
    assert "--appendfsync" in command and "everysec" in command
    assert "--maxmemory" in command and "128mb" in command
    assert "--maxmemory-policy" in command and "volatile-lru" in command
    shared_cache = (ROOT / "services" / "shared_cache.py").read_text(encoding="utf-8")
    assert "client.setex(" in shared_cache
    assert "max(1, int(ttl_seconds))" in shared_cache
    assert "redis-data:/data" in redis["volumes"]


def test_container_build_includes_collector_and_worker_entrypoints():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert (
        "FROM python:3.11.17-slim-trixie@sha256:"
        "e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce"
    ) in dockerfile.splitlines()
    assert "COPY worker_main.py ." in dockerfile
    assert "COPY config.yaml ." in dockerfile
    assert "COPY scoring_provenance.py ." in dockerfile
    assert "COPY scripts/run_event_enrichment.py ./scripts/run_event_enrichment.py" in dockerfile
    assert "COPY scripts/run_provenance_shadow.py ./scripts/run_provenance_shadow.py" in dockerfile
    assert "COPY workers/ ./workers/" in dockerfile
    assert "COPY modules/ ./modules/" in dockerfile
    compile_line = next(line for line in dockerfile.splitlines() if "py_compile" in line)
    assert "worker_main.py" in compile_line
    assert "scoring_provenance.py" in compile_line
    assert "workers/*.py" in compile_line
    assert "modules/*.py" in compile_line
    assert "scripts/run_event_enrichment.py" in compile_line
    assert "scripts/run_provenance_shadow.py" in compile_line


def test_event_enrichment_is_an_immutable_least_secret_one_shot():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    enrichment = services["event-enrichment"]

    assert enrichment["profiles"] == ["cron"]
    assert enrichment["image"] == services["ai-filter-api"]["image"]
    assert enrichment["command"].startswith("python scripts/run_event_enrichment.py ")
    assert "env_file" not in enrichment
    assert "secrets" not in enrichment
    assert set(enrichment["environment"]) == {
        "AI_FILTER_DB_PATH",
        "AI_FILTER_CONFIG_PATH",
        "AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE",
        "AI_FILTER_MAPPING_MIGRATION_REQUIRED_GENERATION",
        "AI_FILTER_SESSION_SECRET",
        "AI_FILTER_ADMISSION_MODE",
        "AI_FILTER_ADMISSION_ROOT",
        "TZ",
    }
    assert enrichment["environment"]["AI_FILTER_ADMISSION_MODE"] == "disabled"
    assert enrichment["environment"]["AI_FILTER_ADMISSION_ROOT"] == "/app/data/admission_fence"
    assert enrichment["environment"]["AI_FILTER_MAPPING_MIGRATION_ENFORCE_GATE"] == "1"
    assert (
        enrichment["environment"]["AI_FILTER_MAPPING_MIGRATION_REQUIRED_GENERATION"]
        == "event-mapping-store-v5-provider-attribution-v1"
    )
    assert enrichment["network_mode"] == "none"
    assert "./data:/app/data" in enrichment["volumes"]
    assert "./config.yaml:/app/config.yaml:ro" not in enrichment["volumes"]
    assert enrichment["restart"] == "no"


def test_market_close_runner_is_packaged_and_executable():
    runner = ROOT / "scripts" / "run_market_close.sh"

    assert runner.stat().st_mode & 0o111
    source = runner.read_text(encoding="utf-8")
    assert "current.state" in source
    assert "market-close.lock" in source
    assert "sha256sum" in source
    assert "--profile cron run --pull never --rm --no-deps" in source


def test_redis_client_version_matches_verified_production_version():
    production = (ROOT / "requirements-prod.txt").read_text(encoding="utf-8").splitlines()
    development = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    assert "redis==8.0.1" in production
    assert "redis==5.2.1" not in production
    assert "-r requirements-prod.txt" in development
    assert not any(line.startswith("redis==") for line in development)


def test_release_waits_for_real_rsshub_health():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]
    rsshub_health = services["rsshub"]["healthcheck"]

    assert rsshub_health["test"][:3] == ["CMD", "node", "-e"]
    assert "http://127.0.0.1:1200/healthz" in rsshub_health["test"][3]
    assert services["ai-filter-api"]["depends_on"]["rsshub"]["condition"] == "service_healthy"
    assert services["ai-filter-web"]["depends_on"]["ai-filter-api"]["condition"] == "service_healthy"
    assert "ai-filter-sse" not in services

    release_script = (ROOT / "scripts" / "release.sh").read_text(encoding="utf-8")
    assert (
        "redis rsshub ai-filter-api ai-filter-web ai-filter-worker"
        in release_script
    )


def test_gateway_config_is_immutable_and_embedded_in_release_manifest():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    gateway = compose["services"]["ai-filter-web"]
    config_mount = gateway["configs"][0]
    config_name = config_mount["source"]
    nginx = compose["configs"][config_name]["content"]

    assert gateway["image"].startswith("nginx:stable-alpine@sha256:")
    assert "volumes" not in gateway
    assert config_mount["target"] == "/etc/nginx/nginx.conf"
    assert "location = /stream" not in nginx
    assert "ai-filter-api:5000" in nginx
