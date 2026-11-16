"""热重载前置校验：评分契约变更不能把已接收的投递永久留在队列里。

投递信封里的 ``receipt_context.strategy`` 一旦与运行态契约不同，claim 循环只会
跳过它：不重试、不进 DLQ、回执永远停在 ``accepted``。管理端发布新契约前必须
先确认队列里没有会在新契约下变成孤儿的工作。
"""

import json
import time
from types import SimpleNamespace

import pytest

import core.config as config_module
import routes.shared as shared
from tests.test_webhook_durable_spool import isolated_spool  # noqa: F401

API_KEY = "reload-gate-test-key"
FEED = "https://example.invalid/reload-gate-feed"

CONFIG_TEMPLATE = """security:
  api_key: {api_key}
threshold:
  pass: {threshold_pass}
  notify: 9
  similarity: 0.42
cache_ttl: {cache_ttl}
max_entries: 15
feeds:
  - url: {feed}
"""


def _write_config(path, *, threshold_pass=7, cache_ttl=600):
    path.write_text(
        CONFIG_TEMPLATE.format(
            api_key=API_KEY,
            threshold_pass=threshold_pass,
            cache_ttl=cache_ttl,
            feed=FEED,
        ),
        encoding="utf-8",
    )


def _task(number=1):
    return {
        "feed_url": FEED,
        "entries": [
            {
                "title": f"entry-{number}",
                "url": f"{FEED}/{number}",
                "content": "synthetic body",
            }
        ],
    }


@pytest.fixture
def reload_gate(tmp_path, monkeypatch, isolated_spool):  # noqa: F811
    webhook = isolated_spool
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(config_path))
    holder = config_module.config
    monkeypatch.setattr(holder, "_cfg", config_module.load_config())

    backend_reloads = []
    monkeypatch.setattr(
        shared.scorer, "reload_backends", lambda: backend_reloads.append(True)
    )

    import app as app_module

    def post_reload():
        with app_module.app.test_client() as client:
            return client.post("/config/reload", headers={"X-API-Key": API_KEY})

    def write_config(**kwargs):
        _write_config(config_path, **kwargs)

    def enqueue(number=1):
        task = _task(number)
        body = json.dumps({"entries": task["entries"]}).encode()
        result, record = webhook._persist_webhook_delivery(task, body)
        assert result == "stored"
        return record

    return SimpleNamespace(
        webhook=webhook,
        holder=holder,
        config_path=config_path,
        backend_reloads=backend_reloads,
        post_reload=post_reload,
        write_config=write_config,
        enqueue=enqueue,
    )


def test_contract_change_with_pending_delivery_is_rejected(reload_gate):
    reload_gate.enqueue()

    reload_gate.write_config(threshold_pass=6)
    response = reload_gate.post_reload()

    assert response.status_code == 409
    assert response.get_json()["undrained_deliveries"] == 1
    assert reload_gate.holder.cfg["threshold"]["pass"] == 7
    assert reload_gate.backend_reloads == []


def test_contract_change_with_inflight_delivery_is_rejected(reload_gate):
    reload_gate.enqueue()
    claimed = reload_gate.webhook._claim_next_spooled_task(now=time.time() + 1)
    assert claimed is not None

    reload_gate.write_config(threshold_pass=6)
    response = reload_gate.post_reload()

    assert response.status_code == 409
    assert response.get_json()["undrained_deliveries"] == 1
    assert reload_gate.holder.cfg["threshold"]["pass"] == 7


def test_contract_change_is_published_after_pending_work_finishes(reload_gate):
    reload_gate.enqueue()
    directory = reload_gate.webhook._ensure_spool_dir()
    with reload_gate.webhook._locked_spool() as locked:
        assert locked == directory
        pending = list(directory.glob("*.json"))
        assert len(pending) == 1
        # 模拟执行器已完成并删除队列文件（真实完成还需要模型）。
        pending[0].unlink()

    reload_gate.write_config(threshold_pass=6)
    response = reload_gate.post_reload()

    assert response.status_code == 200
    assert reload_gate.holder.cfg["threshold"]["pass"] == 6


def test_non_scoring_change_reloads_with_pending_delivery(reload_gate):
    reload_gate.enqueue()

    reload_gate.write_config(cache_ttl=1200)
    response = reload_gate.post_reload()

    assert response.status_code == 200
    assert reload_gate.holder.cfg["cache_ttl"] == 1200
    assert reload_gate.backend_reloads == [True]
    # 契约没变，排队中的投递仍然可以被 claim。
    assert reload_gate.webhook._claim_next_spooled_task(now=time.time() + 1) is not None


def test_internal_spool_tasks_do_not_block_contract_change(reload_gate):
    assert reload_gate.webhook._persist_webhook_task({"task_type": "refresh"}) == "stored"

    reload_gate.write_config(threshold_pass=6)
    response = reload_gate.post_reload()

    assert response.status_code == 200
    assert reload_gate.holder.cfg["threshold"]["pass"] == 6


def test_unreadable_spool_fails_closed(reload_gate, monkeypatch):
    def unavailable(_strategy):
        return None

    monkeypatch.setattr(
        reload_gate.webhook, "undrained_delivery_count", unavailable
    )

    reload_gate.write_config(threshold_pass=6)
    response = reload_gate.post_reload()

    assert response.status_code == 503
    assert reload_gate.holder.cfg["threshold"]["pass"] == 7
    assert reload_gate.backend_reloads == []


def test_contract_resolution_failure_fails_closed(reload_gate, monkeypatch):
    import routes.admin_routes as admin_routes

    def broken(*_args, **_kwargs):
        raise RuntimeError("feedback profile unavailable")

    monkeypatch.setattr(admin_routes, "resolve_scoring_contract", broken)

    response = reload_gate.post_reload()

    assert response.status_code == 503
    assert reload_gate.backend_reloads == []


def test_reload_still_requires_api_key(reload_gate):
    import app as app_module

    reload_gate.write_config(threshold_pass=6)
    with app_module.app.test_client() as client:
        response = client.post("/config/reload")

    assert response.status_code == 401
    assert reload_gate.holder.cfg["threshold"]["pass"] == 7
