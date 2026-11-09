"""热重载的作用域：只重载处理请求的进程，且要如实报告改了什么。

生产镜像里的 ``config.yaml`` 是镜像内容（``Dockerfile`` 的 ``COPY``），容器没有
挂载它，环境变量也在创建时固定，所以生产里 ``POST /config/reload`` 读到的永远是
与原启动相同的字节。拆分布署（``AI_FILTER_ROLE=web``）下更严重：worker 进程没有
任何重载入口，若 API 单独切换评分契约，此后接收的投递会带新策略，而 worker 仍按
旧契约 claim，永远认领不上。这里把「未变更」与「只作用于本进程」如实写进响应，
并拒绝拆分布署下的契约切换。
"""

import json
from types import SimpleNamespace

import pytest

import core.config as config_module
import routes.shared as shared
from tests.test_webhook_durable_spool import isolated_spool  # noqa: F401

API_KEY = "reload-scope-test-key"
FEED = "https://example.invalid/reload-scope-feed"

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


@pytest.fixture
def reload_scope(tmp_path, monkeypatch, isolated_spool):  # noqa: F811
    webhook = isolated_spool
    config_path = tmp_path / "config.yaml"
    _write_config(config_path)
    monkeypatch.setattr(config_module, "CONFIG_PATH", str(config_path))
    holder = config_module.config
    monkeypatch.setattr(holder, "_cfg", config_module.load_config())
    monkeypatch.delenv("AI_FILTER_ROLE", raising=False)

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

    return SimpleNamespace(
        webhook=webhook,
        holder=holder,
        config_path=config_path,
        backend_reloads=backend_reloads,
        post_reload=post_reload,
        write_config=write_config,
    )


def _body(response):
    return json.loads(response.get_data(as_text=True))


def test_unchanged_config_is_reported_as_unchanged(reload_scope):
    """原样重载不能回一句「配置已重新加载」让人以为有新内容生效。"""
    response = reload_scope.post_reload()

    assert response.status_code == 200
    body = _body(response)
    assert body["changed"] is False
    assert body["scope"] == "all-processes"
    # 只声明配置值未变：后端与运行态缓存仍会被刷新，文案不能把两者混为一谈。
    assert "配置值与当前进程一致" in body["message"]
    assert "已重新加载" not in body["message"]
    assert reload_scope.backend_reloads == [True]


def test_changed_config_is_reported_and_applied(reload_scope):
    reload_scope.write_config(cache_ttl=1200)

    response = reload_scope.post_reload()

    assert response.status_code == 200
    body = _body(response)
    assert body["changed"] is True
    assert body["scope"] == "all-processes"
    assert reload_scope.holder.cfg["cache_ttl"] == 1200
    assert reload_scope.backend_reloads == [True]


def test_split_role_contract_change_is_refused(reload_scope, monkeypatch):
    """worker 不重载：API 单独切契约会让之后接收的投递永远认领不上。"""
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    reload_scope.write_config(threshold_pass=6)

    response = reload_scope.post_reload()

    assert response.status_code == 409
    body = _body(response)
    assert body["status"] == "conflict"
    assert body["reason"] == "split_deployment"
    assert body["changed"] is True
    assert "worker" in body["message"]
    assert reload_scope.holder.cfg["threshold"]["pass"] == 7
    assert reload_scope.backend_reloads == []


def test_split_role_reload_reports_api_only_scope(reload_scope, monkeypatch):
    """非契约变更可以生效，但必须说明只有 API 进程被刷新。"""
    monkeypatch.setenv("AI_FILTER_ROLE", "web")
    reload_scope.write_config(cache_ttl=1200)

    response = reload_scope.post_reload()

    assert response.status_code == 200
    body = _body(response)
    assert body["changed"] is True
    assert body["scope"] == "api-process"
    assert body["worker_reloaded"] is False
    assert reload_scope.holder.cfg["cache_ttl"] == 1200
    assert reload_scope.backend_reloads == [True]


def test_split_role_unchanged_config_reports_the_boundary(reload_scope, monkeypatch):
    monkeypatch.setenv("AI_FILTER_ROLE", "web")

    response = reload_scope.post_reload()

    assert response.status_code == 200
    body = _body(response)
    assert body["changed"] is False
    assert body["scope"] == "api-process"
    assert body["worker_reloaded"] is False


def test_single_process_contract_change_still_applies(reload_scope):
    """role=all（本地/单进程）没有别的 worker，契约切换仍然可用。"""
    reload_scope.write_config(threshold_pass=6)

    response = reload_scope.post_reload()

    assert response.status_code == 200
    body = _body(response)
    assert body["changed"] is True
    assert body["scope"] == "all-processes"
    assert reload_scope.holder.cfg["threshold"]["pass"] == 6
    assert reload_scope.backend_reloads == [True]
