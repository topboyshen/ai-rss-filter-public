from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_every_application_data_mount_masks_release_registry_read_only():
    compose = yaml.safe_load(
        (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    masked_services = set()

    for name, service in compose["services"].items():
        volumes = service.get("volumes") or []
        if "./data:/app/data" not in volumes:
            continue
        assert "release-registry-mask:/app/data/releases:ro" in volumes, name
        masked_services.add(name)

    assert masked_services == {
        "ai-filter-api",
        "ai-filter-worker",
        "market-close-collector",
        "event-enrichment",
        "security-master-sync",
    }
    assert "release-registry-mask" in compose["volumes"]
