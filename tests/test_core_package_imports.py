"""Package imports must not silently initialize the entire runtime chain."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_isolated_import(tmp_path, script):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("threshold:\n  pass: 7\nfeeds: []\n", encoding="utf-8")
    db_path = tmp_path / "import-only.db"
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("AI_FILTER_", "LLM_", "KIMI_", "TELEGRAM_"))}
    env.update({
        "AI_FILTER_CONFIG_PATH": str(config_path),
        "AI_FILTER_DB_PATH": str(db_path),
        "AI_FILTER_ROLE": "web",
        "AI_FILTER_REDIS_URL": "",
    })
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=PROJECT_ROOT, env=env,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not db_path.exists()
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()


@pytest.mark.parametrize("module", ["core", "core.asset_mapping", "core.score_evidence"])
def test_package_and_pure_rules_do_not_load_runtime_chain(tmp_path, module):
    _run_isolated_import(tmp_path, f"""
import importlib
import sys
importlib.import_module({module!r})
for forbidden in ('core.config', 'core.scorer', 'core.processor', 'core.notifier',
                  'openai', 'db.database'):
    assert forbidden not in sys.modules, forbidden
""")


def test_config_and_prompt_submodules_keep_explicit_interfaces(tmp_path):
    _run_isolated_import(tmp_path, """
import sys
import core.config as config_module
from core.config import config, load_config
from core.prompts import get_prompt, get_prompt_for_url
assert config_module.config is config
assert load_config()['threshold']['pass'] == config.cfg['threshold']['pass'] == 7
cfg = {'prompts': {'custom': 'fixed prompt'},
       'prompt_matchers': [{'pattern': 'example', 'key': 'custom'}]}
assert get_prompt('custom', cfg) == 'fixed prompt'
assert get_prompt_for_url('https://example.test/feed', cfg) == 'fixed prompt'
for forbidden in ('core.scorer', 'core.processor', 'core.notifier', 'openai'):
    assert forbidden not in sys.modules, forbidden
""")


def test_from_core_import_submodules_and_pipeline_interfaces_still_work(tmp_path):
    _run_isolated_import(tmp_path, """
from core import processor, asset_mapping
from core.processor import process_feed, build_rss_xml, process_entry
from core.scorer import Scorer
from core.notifier import send_telegram_notification
assert processor.process_feed is process_feed
assert processor.build_rss_xml is build_rss_xml
assert processor.process_entry is process_entry
assert processor.Scorer is Scorer
assert processor.send_telegram_notification is send_telegram_notification
assert asset_mapping.get_mapping_version().startswith('asset-map:title-only-v2:')
""")


def test_api_and_worker_import_without_database_initialization(tmp_path):
    _run_isolated_import(tmp_path, """
import app
import worker_main
from core.config import config
assert app.config is config
assert callable(app.initialize_app)
assert callable(worker_main.main)
response = app.app.test_client().get('/health')
assert response.status_code == 503  # No browser credentials: retain fail-closed health.
assert response.data == b'NOT READY'
""")


def test_deterministic_evidence_rules_keep_frozen_behavior(tmp_path):
    _run_isolated_import(tmp_path, """
from core.score_evidence import explicit_event_claims_are_grounded, normalize_money_units
assert normalize_money_units('甲公司订单3亿元') == '甲公司订单300000000元'
assert explicit_event_claims_are_grounded('甲公司获得订单', '甲公司获得订单')
assert not explicit_event_claims_are_grounded('甲公司获得订单', '甲公司未获得订单')
assert explicit_event_claims_are_grounded('甲公司计划获得订单', '甲公司计划获得订单')
assert not explicit_event_claims_are_grounded('甲公司获得订单', '甲公司计划获得订单')
""")
