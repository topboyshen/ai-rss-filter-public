"""Synthetic helper boundaries; these tests make no actual PG migration claim."""
from pathlib import Path

import pytest

from scripts import production_startup_pg_fixture as fixture
from services.sender_drain_contract import DrainContractError


@pytest.mark.parametrize("dsn", [
    "postgres://localhost/production", "postgres://production.invalid/synthetic_outbox",
    "postgres://localhost/synthetic_outbox?host=production.invalid",
    "postgres://localhost/synthetic_outbox?options=-c%20search_path=public",
])
def test_external_or_overridden_database_rejected_before_import_or_connect(dsn):
    with pytest.raises(DrainContractError):
        fixture.create_fixture(dsn, Path("/nonexistent-source"))


def test_source_missing_or_different_is_not_silently_replaced(tmp_path):
    with pytest.raises(FileNotFoundError):
        fixture.create_fixture("postgres://localhost/synthetic_outbox", tmp_path)
    source = tmp_path / fixture.DDL[0][0]
    source.parent.mkdir(parents=True)
    source.write_text("not the locked reviewed SQL")
    with pytest.raises(DrainContractError, match="source hash differs"):
        fixture.create_fixture("postgres://localhost/synthetic_outbox", tmp_path)
