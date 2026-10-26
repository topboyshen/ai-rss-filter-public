"""Keep the legacy collector from pretending an anonymous scrape is usable."""

from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_optional_prometheus_template_has_no_unauthenticated_scrape_jobs():
    path = PROJECT_ROOT / "monitoring" / "prometheus.yml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert config["scrape_configs"] == []
    assert config["global"]["scrape_interval"] == "15s"


def test_optional_stack_is_not_a_default_production_service():
    path = PROJECT_ROOT / "docker-compose.yml"
    services = yaml.safe_load(path.read_text(encoding="utf-8"))["services"]

    assert "prometheus" not in services
    assert "grafana" not in services
