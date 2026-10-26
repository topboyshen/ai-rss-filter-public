import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _compose_services():
    compose = yaml.safe_load(
        (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    return compose["services"]


def test_compose_bounds_memory_and_processes_for_every_service():
    services = _compose_services()
    expected_limits = {
        "redis": ("384m", 128),
        "ai-filter-api": ("768m", 256),
        "ai-filter-web": ("128m", 64),
        "ai-filter-worker": ("768m", 256),
        "market-close-collector": ("384m", 128),
        "event-enrichment": ("768m", 256),
        "security-master-sync": ("384m", 128),
        "rsshub": ("768m", 256),
    }

    assert set(services) == set(expected_limits)
    for service_name, (memory, pids) in expected_limits.items():
        service = services[service_name]
        assert service.get("mem_limit") == memory, service_name
        assert service.get("pids_limit") == pids, service_name


def test_api_healthcheck_is_lightweight_and_stop_window_covers_gunicorn_grace():
    api = _compose_services()["ai-filter-api"]
    health_command = " ".join(api["healthcheck"]["test"])

    assert api["stop_grace_period"] == "45s"
    assert api["healthcheck"]["start_period"] == "90s"
    assert "http://127.0.0.1:5000/health" in health_command
    assert "browser_login_ready" not in health_command
    assert "routes." not in health_command


def test_compose_prevents_privilege_escalation_for_every_service():
    for service_name, service in _compose_services().items():
        security_options = set(service.get("security_opt") or [])
        assert "no-new-privileges:true" in security_options, service_name


def test_compose_projects_only_explicit_role_credentials_without_env_file():
    services = _compose_services()
    expected = {
        "redis": set(),
        "ai-filter-api": {
            "AI_FILTER_API_KEY",
            "KIMI_CODE_API_KEY",
            "LLM_API_KEY",
            "MINIFLUX_WEBHOOK_SECRET",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
        },
        "ai-filter-web": set(),
        "ai-filter-worker": {
            "AI_FILTER_API_KEY",
            "KIMI_CODE_API_KEY",
            "LLM_API_KEY",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
        },
        "market-close-collector": set(),
        "event-enrichment": set(),
        "security-master-sync": set(),
        "rsshub": set(),
    }

    for service_name, service in services.items():
        assert "env_file" not in service, service_name
        interpolated = {
            name
            for name, value in (service.get("environment") or {}).items()
            if isinstance(value, str) and value.startswith("${")
        }
        assert interpolated == expected[service_name], service_name

    assert (
        services["ai-filter-api"]["environment"]["PUBLIC_BASE_URL"]
        == "https://example.invalid"
    )


def test_env_example_contains_only_projected_credential_plane():
    lines = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assert "仅用它做显式变量插值" in lines[0]
    assert "若用 env_file" not in "\n".join(lines)
    assignments = {
        line.split("=", 1)[0]
        for line in lines
        if line and not line.startswith("#") and "=" in line
    }
    assert assignments == {
        "AI_FILTER_API_KEY",
        "KIMI_CODE_API_KEY",
        "LLM_API_KEY",
        "MINIFLUX_WEBHOOK_SECRET",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    }


def _load_gunicorn_config():
    path = PROJECT_ROOT / "gunicorn.conf.py"
    spec = importlib.util.spec_from_file_location("test_gunicorn_config", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_start_script_does_not_require_only_environment_key():
    script = (PROJECT_ROOT / "start.sh").read_text(encoding="utf-8")
    assert "AI_FILTER_API_KEY is required" not in script


def test_importing_app_does_not_initialize_database(tmp_path):
    db_path = tmp_path / "import-must-not-create.db"
    env = os.environ.copy()
    env["AI_FILTER_DB_PATH"] = str(db_path)

    result = subprocess.run(
        [sys.executable, "-c", "import app"],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not db_path.exists()
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()


def test_scoring_provenance_is_explicitly_packaged_without_copying_context():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY scoring_provenance.py ." in dockerfile
    assert "COPY . ." not in dockerfile
    compile_line = next(
        line for line in dockerfile.splitlines() if "py_compile" in line
    )
    assert "scoring_provenance.py" in compile_line


@pytest.mark.parametrize("script", [
    "export_score_gold_candidates.py", "import_score_gold_labels.py", "score_gold_metrics.py",
])
def test_gold_cli_is_explicitly_packaged_and_compiled(script):
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f"COPY scripts/{script} ./scripts/{script}" in dockerfile
    compile_line = next(line for line in dockerfile.splitlines() if "py_compile" in line)
    assert f"scripts/{script}" in compile_line
    assert "COPY . ." not in dockerfile


@pytest.mark.parametrize(
    "imports",
    [
        "import db.database; import core.scorer; import core.processor",
        "import core.scorer; import db.database; import services.quality_bridge",
    ],
)
def test_real_scoring_provenance_import_order_is_cycle_free(
    tmp_path, imports,
):
    db_path = tmp_path / "import-smoke.db"
    env = os.environ.copy()
    env["AI_FILTER_DB_PATH"] = str(db_path)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            imports
            + "; from scoring_provenance import NON_SUBSTANTIVE_SCORE_PATHS"
            + "; assert len(NON_SUBSTANTIVE_SCORE_PATHS) == 6",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not db_path.exists()


def test_worker_start_explicitly_initializes_database(monkeypatch):
    import app as app_module
    import routes.shared as shared
    import services.shared_cache as shared_cache

    calls = []
    monkeypatch.setattr(shared, "_security_cfg", lambda: {"api_key": "from-yaml"})
    monkeypatch.setattr(shared_cache, "process_role", lambda: "web")
    monkeypatch.setattr(app_module, "initialize_app", lambda: calls.append("database"))

    module = _load_gunicorn_config()
    module.post_worker_init(object())

    assert calls == ["database"]


def test_worker_init_fails_before_threads_without_effective_key(monkeypatch):
    import routes.shared as shared
    import routes.dashboard_routes as dashboard
    import routes.webhook_routes as webhook_routes

    calls = []
    monkeypatch.setattr(shared, "_security_cfg", lambda: {"api_key": ""})
    monkeypatch.setattr(
        dashboard,
        "_ensure_quotes_refresher",
        lambda: calls.append("quotes"),
    )
    monkeypatch.setattr(
        webhook_routes,
        "start_webhook_dispatcher",
        lambda: calls.append("dispatcher"),
    )

    module = _load_gunicorn_config()
    with pytest.raises(RuntimeError, match="security.api_key"):
        module.post_worker_init(object())

    assert calls == []


def test_worker_init_web_role_skips_background_threads(monkeypatch):
    import routes.shared as shared
    import routes.dashboard_routes as dashboard
    import routes.webhook_routes as webhook_routes
    import services.shared_cache as shared_cache

    calls = []
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    monkeypatch.setattr(shared, "_security_cfg", lambda: {"api_key": "from-yaml"})
    monkeypatch.setattr(shared_cache, "process_role", lambda: "web")
    monkeypatch.setattr(
        dashboard,
        "_ensure_quotes_refresher",
        lambda: calls.append("quotes"),
    )
    monkeypatch.setattr(
        webhook_routes,
        "start_webhook_dispatcher",
        lambda: calls.append("dispatcher"),
    )

    module = _load_gunicorn_config()
    module.post_worker_init(object())

    assert calls == []


def test_worker_init_all_role_starts_background_threads(monkeypatch):
    import routes.shared as shared
    import routes.dashboard_routes as dashboard
    import routes.webhook_routes as webhook_routes
    import services.shared_cache as shared_cache

    calls = []
    monkeypatch.setattr(shared, "_security_cfg", lambda: {"api_key": "from-yaml"})
    monkeypatch.setattr(shared_cache, "process_role", lambda: "all")
    monkeypatch.setattr(
        dashboard,
        "_ensure_quotes_refresher",
        lambda: calls.append("quotes"),
    )
    monkeypatch.setattr(
        webhook_routes,
        "start_webhook_dispatcher",
        lambda: calls.append("dispatcher"),
    )

    module = _load_gunicorn_config()
    module.post_worker_init(object())

    assert calls == ["quotes", "dispatcher"]
