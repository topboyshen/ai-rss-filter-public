import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _defined_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_worker_has_no_ingestion_digest_or_event_projector_scheduler():
    source = (ROOT / "worker_main.py").read_text(encoding="utf-8")

    assert "auto_refresh" not in source
    assert "digest_scheduler" not in source
    assert "event_enrichment" not in source
    assert "heartbeat" not in source


def test_refresh_is_not_a_scheduler_or_event_projector():
    path = ROOT / "services" / "refresh.py"
    source = path.read_text(encoding="utf-8")
    definitions = _defined_functions(path)

    assert "start_auto_refresh_if_configured" not in definitions
    assert "stop_auto_refresh" not in definitions
    assert "start_heartbeat" not in definitions
    assert "enrich_and_resolve_recent_events" not in source


def test_digest_has_no_scheduler_api_or_background_loop():
    definitions = _defined_functions(ROOT / "services" / "digest.py")

    assert "run_daily_digest_scheduler_once" not in definitions
    assert "start_digest_scheduler" not in definitions
    assert "stop_digest_scheduler" not in definitions
