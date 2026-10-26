import json
import sqlite3

import pytest

from db.event_core import init_event_core_schema, upsert_security
from services import security_master_sync as sync


def _row(code, name, *, category="A股", pinyin="name", org_id="gssz0000001"):
    return {
        "code": code,
        "zwjc": name,
        "category": category,
        "pinyin": pinyin,
        "orgId": org_id,
    }


def _payload(*rows):
    return json.dumps({"stockList": list(rows)}, ensure_ascii=False).encode()


class _Response:
    def __init__(self, payload, url=sync.CNINFO_STOCK_URL):
        self.payload = payload
        self.url = url
        self.read_limit = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read(self, limit):
        self.read_limit = limit
        return self.payload[:limit]


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    init_event_core_schema(connection)
    try:
        yield connection
    finally:
        connection.close()


def test_cninfo_parser_accepts_only_a_shares_and_maps_all_three_exchanges():
    candidates = sync.parse_cninfo_snapshot(
        _payload(
            _row("000001", "平安银行", pinyin="payh"),
            _row("300750", "宁德时代", pinyin="ndsd"),
            _row("600519", "贵州茅台", pinyin="gzmt", org_id="gssh0600519"),
            _row("430017", "星昊医药", pinyin="xhyy", org_id="gfbj0430017"),
            _row("830799", "艾融软件", pinyin="arrj", org_id="gfbj0830799"),
            _row("920000", "安徽凤凰", pinyin="ahfh", org_id="gfbj0920000"),
            _row("200001", "深物业B", category="B股"),
        ),
        minimum_a_shares=6,
    )

    assert [(item.exchange, item.ticker) for item in candidates] == [
        ("BSE", "430017"),
        ("BSE", "830799"),
        ("BSE", "920000"),
        ("SSE", "600519"),
        ("SZSE", "000001"),
        ("SZSE", "300750"),
    ]
    assert candidates[2].quote_symbol == "bj920000"
    assert "920000.BJ" in candidates[2].aliases
    assert "payh" not in next(
        item.aliases for item in candidates if item.ticker == "000001"
    )


def test_cninfo_parser_collapses_bse_legacy_code_into_920_aliases():
    candidates = sync.parse_cninfo_snapshot(
        _payload(
            _row(
                "832000",
                "安徽凤凰旧码",
                pinyin="ahfh",
                org_id="gfbj0832000",
            ),
            _row(
                "920000",
                "安徽凤凰",
                pinyin="ahfh",
                org_id="gfbj0832000",
            ),
        ),
        minimum_a_shares=1,
    )

    assert [(item.exchange, item.ticker, item.name) for item in candidates] == [
        ("BSE", "920000", "安徽凤凰")
    ]
    assert set(candidates[0].aliases) == {
        "832000",
        "832000.BJ",
        "920000",
        "920000.BJ",
        "BJ832000",
        "BJ920000",
    }
    assert candidates[0].source_org_id == "gfbj0832000"


@pytest.mark.parametrize(
    "row",
    (
        _row("９２００００", "全角代码"),
        _row("92000", "短代码"),
        _row("700001", "未知代码段"),
        _row("000001", ""),
        _row("000001", "平安银行", org_id="bad id"),
        _row("000001", "平安银行", pinyin="拼音"),
        _row(1, "数字代码"),
        _row("000001", 123),
        _row("000001", "平安银行", pinyin=123),
    ),
)
def test_cninfo_parser_fails_closed_on_malformed_a_share(row):
    with pytest.raises(sync.SecurityMasterValidationError):
        sync.parse_cninfo_snapshot(_payload(row), minimum_a_shares=0)


def test_cninfo_parser_rejects_conflicting_duplicate_and_incomplete_snapshot():
    with pytest.raises(sync.SecurityMasterValidationError, match="conflicting"):
        sync.parse_cninfo_snapshot(
            _payload(_row("000001", "平安银行"), _row("000001", "另一个名称")),
            minimum_a_shares=1,
        )
    with pytest.raises(sync.SecurityMasterValidationError, match="below"):
        sync.parse_cninfo_snapshot(_payload(_row("000001", "平安银行")))


def test_cninfo_parser_enforces_per_exchange_completeness_floors():
    one_market = [
        _row(f"{number:06d}", f"深市证券{number}", org_id=f"org{number}")
        for number in range(sync.MIN_CNINFO_A_SHARE_RECORDS)
    ]
    with pytest.raises(sync.SecurityMasterValidationError, match="SSE"):
        sync.parse_cninfo_snapshot(
            _payload(*one_market),
            minimum_a_shares=sync.MIN_CNINFO_A_SHARE_RECORDS,
        )
    for invalid_minimum in (True, -1, sync.MAX_CNINFO_RECORDS + 1):
        with pytest.raises(ValueError):
            sync.parse_cninfo_snapshot(_payload(), minimum_a_shares=invalid_minimum)


def test_fetch_is_fixed_bounded_and_rejects_redirect():
    response = _Response(_payload(_row("000001", "平安银行")))

    def opener(request, timeout):
        assert request.full_url == sync.CNINFO_STOCK_URL
        assert timeout == 15
        return response

    candidates = sync.fetch_cninfo_candidates(minimum_a_shares=1, opener=opener)
    assert len(candidates) == 1
    assert response.read_limit == sync.MAX_CNINFO_RESPONSE_BYTES + 1

    redirected = _Response(
        _payload(_row("000001", "平安银行")),
        url="https://evil.example/stock.json",
    )
    with pytest.raises(sync.SecurityMasterFetchError, match="redirected"):
        sync.fetch_cninfo_candidates(
            minimum_a_shares=1,
            opener=lambda *_args, **_kwargs: redirected,
        )


def test_fetch_rejects_oversized_response():
    oversized = _Response(b"x" * (sync.MAX_CNINFO_RESPONSE_BYTES + 1))
    with pytest.raises(sync.SecurityMasterFetchError, match="size"):
        sync.fetch_cninfo_candidates(
            minimum_a_shares=0,
            opener=lambda *_args, **_kwargs: oversized,
        )


def test_default_fetcher_disables_ambient_proxies_and_redirects(monkeypatch):
    response = _Response(_payload(_row("000001", "平安银行")))
    seen_handlers = []

    class BuiltOpener:
        def open(self, request, timeout):
            assert request.get_header("Accept-encoding") == "identity"
            assert timeout == 15
            return response

    def build_opener(*handlers):
        seen_handlers.extend(handlers)
        return BuiltOpener()

    monkeypatch.setattr(sync.urllib.request, "build_opener", build_opener)
    assert len(sync.fetch_cninfo_candidates(minimum_a_shares=1)) == 1
    assert any(
        isinstance(handler, sync.urllib.request.ProxyHandler)
        and handler.proxies == {}
        for handler in seen_handlers
    )
    assert any(isinstance(handler, sync._RejectRedirects) for handler in seen_handlers)


def test_tencent_confirmation_uses_explicit_exchange_symbols_and_current_name():
    candidates = sync.parse_cninfo_snapshot(
        _payload(
            _row("000001", "平安银行旧名"),
            _row("600519", "贵州茅台", org_id="gssh0600519"),
            _row("920000", "安徽凤凰", org_id="gfbj0920000"),
        ),
        minimum_a_shares=3,
    )
    seen = []

    def quotes(symbols):
        seen.extend(symbols)
        return {
            symbol[2:]: {
                "symbol": symbol,
                "name": {
                    "sz000001": "平安银行",
                    "sh600519": "贵州茅台",
                    "bj920000": "安徽凤凰",
                }[symbol],
                "quote_ts": 100,
            }
            for symbol in symbols
        }

    confirmed, stats = sync.confirm_candidates_with_tencent(
        candidates,
        quote_fetcher=quotes,
        minimum_ratio=1,
        minimum_exchange_ratio=1,
        now=100,
    )

    assert set(seen) == {"sz000001", "sh600519", "bj920000"}
    assert stats["confirmed_count"] == 3
    bank = next(item for item in confirmed if item.ticker == "000001")
    assert bank.name == "平安银行旧名"
    assert "平安银行" not in bank.aliases
    assert stats["name_mismatch_count"] == 1


def test_tencent_confirmation_rejects_low_completeness_and_symbol_mismatch():
    candidates = [
        sync.SecurityCandidate("SZSE", f"0000{number:02d}", f"证券{number}")
        for number in range(10)
    ]
    with pytest.raises(sync.SecurityMasterValidationError, match="ratio"):
        sync.confirm_candidates_with_tencent(candidates, quote_fetcher=lambda _symbols: {})

    one = [sync.SecurityCandidate("SSE", "600519", "贵州茅台")]
    with pytest.raises(sync.SecurityMasterValidationError, match="mismatch"):
        sync.confirm_candidates_with_tencent(
            one,
            quote_fetcher=lambda _symbols: {
                "600519": {"symbol": "sz600519", "name": "贵州茅台"}
            },
            minimum_ratio=0,
        )


def test_tencent_confirmation_requires_a_fresh_integer_quote_timestamp():
    candidate = [sync.SecurityCandidate("SSE", "600519", "贵州茅台")]

    def quote(quote_ts):
        return lambda _symbols: {
            "600519": {
                "symbol": "sh600519",
                "name": "贵州茅台",
                "quote_ts": quote_ts,
            }
        }

    for invalid_ts in (None, True, "100", 79, 401):
        with pytest.raises(sync.SecurityMasterValidationError, match="ratio"):
            sync.confirm_candidates_with_tencent(
                candidate,
                quote_fetcher=quote(invalid_ts),
                minimum_ratio=1,
                max_quote_age_seconds=20,
                now=100,
            )
    confirmed, _stats = sync.confirm_candidates_with_tencent(
        candidate,
        quote_fetcher=quote(80),
        minimum_ratio=1,
        max_quote_age_seconds=20,
        now=100,
    )
    assert confirmed == candidate


def test_tencent_confirmation_rejects_duplicate_or_noncanonical_candidates():
    candidate = sync.SecurityCandidate("SSE", "600519", "贵州茅台")
    with pytest.raises(sync.SecurityMasterValidationError, match="duplicate"):
        sync.confirm_candidates_with_tencent(
            [candidate, candidate],
            quote_fetcher=lambda _symbols: {},
            minimum_ratio=0,
        )
    with pytest.raises(sync.SecurityMasterValidationError, match="canonical"):
        sync.confirm_candidates_with_tencent(
            [sync.SecurityCandidate("sse", "600519", "贵州茅台")],
            quote_fetcher=lambda _symbols: {},
            minimum_ratio=0,
        )


def test_sync_is_insert_only_idempotent_and_does_not_create_event_mapping(conn):
    candidate = sync.SecurityCandidate(
        "SSE", "600519", "贵州茅台", ("600519", "SH600519", "gzmt")
    )
    first = sync.sync_confirmed_candidates(conn, [candidate], now=100)
    row_before = conn.execute(
        "SELECT security_id, name, asset_type, currency, aliases, industry, concepts, "
        "is_active, version, created_at, updated_at FROM securities"
    ).fetchone()
    second = sync.sync_confirmed_candidates(conn, [candidate], now=200)
    row_after = conn.execute(
        "SELECT security_id, name, asset_type, currency, aliases, industry, concepts, "
        "is_active, version, created_at, updated_at FROM securities"
    ).fetchone()

    assert first["inserted"] == 1
    assert second == {
        "inserted": 0,
        "unchanged": 1,
        "manual_conflicts": 0,
        "legacy_conflicts": 0,
        "preserved": 1,
    }
    assert row_before == row_after
    assert row_after[2:4] == ("equity", "CNY")
    assert row_after[5:9] == ("", "[]", 1, 1)
    assert conn.execute("SELECT count(*) FROM event_securities").fetchone()[0] == 0


def test_sync_preserves_all_existing_metadata_and_reports_conflict(conn):
    security_id = upsert_security(
        conn,
        exchange="SSE",
        ticker="600519",
        name="人工名称",
        aliases=["人工别名"],
        industry="人工行业",
        concepts=["人工概念"],
        is_active=False,
        now=50,
    )
    before = conn.execute(
        "SELECT * FROM securities WHERE security_id = ?", (security_id,)
    ).fetchone()

    stats = sync.sync_confirmed_candidates(
        conn,
        [sync.SecurityCandidate("SSE", "600519", "贵州茅台", ("gzmt",))],
        now=100,
    )
    after = conn.execute(
        "SELECT * FROM securities WHERE security_id = ?", (security_id,)
    ).fetchone()

    assert stats == {
        "inserted": 0,
        "unchanged": 0,
        "manual_conflicts": 1,
        "legacy_conflicts": 0,
        "preserved": 1,
    }
    assert after == before


def test_sync_does_not_reactivate_an_inactive_existing_security(conn):
    security_id = upsert_security(
        conn,
        exchange="SSE",
        ticker="600519",
        name="贵州茅台",
        is_active=False,
        now=50,
    )
    before = conn.execute(
        "SELECT * FROM securities WHERE security_id = ?", (security_id,)
    ).fetchone()
    stats = sync.sync_confirmed_candidates(
        conn,
        [sync.SecurityCandidate("SSE", "600519", "贵州茅台")],
        now=100,
    )
    after = conn.execute(
        "SELECT * FROM securities WHERE security_id = ?", (security_id,)
    ).fetchone()

    assert stats["manual_conflicts"] == 1
    assert stats["unchanged"] == 0
    assert after == before


def test_sync_does_not_duplicate_an_existing_bse_legacy_identity(conn):
    legacy_id = upsert_security(
        conn,
        exchange="BSE",
        ticker="832000",
        name="安徽凤凰旧码",
        now=50,
    )
    candidate = sync.SecurityCandidate(
        "BSE",
        "920000",
        "安徽凤凰",
        (
            "832000",
            "832000.BJ",
            "920000",
            "920000.BJ",
            "BJ832000",
            "BJ920000",
        ),
        "gfbj0832000",
    )

    stats = sync.sync_confirmed_candidates(conn, [candidate], now=100)

    assert stats["inserted"] == 0
    assert stats["manual_conflicts"] == 1
    assert stats["legacy_conflicts"] == 1
    assert conn.execute(
        "SELECT security_id FROM securities WHERE exchange = 'BSE'"
    ).fetchall() == [(legacy_id,)]


def test_production_runner_does_not_allow_lowering_completeness_floor(conn):
    with pytest.raises(ValueError, match="production"):
        sync.run_security_master_sync(conn, minimum_a_shares=0)


def test_sync_dry_run_and_transaction_rollback(conn, monkeypatch):
    candidates = [
        sync.SecurityCandidate("SZSE", "000001", "平安银行"),
        sync.SecurityCandidate("SSE", "600519", "贵州茅台"),
    ]
    dry = sync.sync_confirmed_candidates(conn, candidates, dry_run=True, now=100)
    assert dry["inserted"] == 2
    assert conn.execute("SELECT count(*) FROM securities").fetchone()[0] == 0

    real_insert = sync._insert_security
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected failure")
        return real_insert(*args, **kwargs)

    monkeypatch.setattr(sync, "_insert_security", fail_second)
    with pytest.raises(RuntimeError, match="injected"):
        sync.sync_confirmed_candidates(conn, candidates, now=100)
    assert conn.execute("SELECT count(*) FROM securities").fetchone()[0] == 0


def test_sync_rollback_is_scoped_inside_caller_transaction(conn, monkeypatch):
    conn.execute("BEGIN")
    prior_id = upsert_security(
        conn,
        exchange="BSE",
        ticker="920000",
        name="调用方已有写入",
        now=50,
    )
    candidates = [
        sync.SecurityCandidate("SZSE", "000001", "平安银行"),
        sync.SecurityCandidate("SSE", "600519", "贵州茅台"),
    ]
    real_insert = sync._insert_security
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected failure")
        return real_insert(*args, **kwargs)

    monkeypatch.setattr(sync, "_insert_security", fail_second)
    with pytest.raises(RuntimeError, match="injected"):
        sync.sync_confirmed_candidates(conn, candidates, now=100)

    assert conn.in_transaction
    assert conn.execute(
        "SELECT security_id FROM securities WHERE exchange = 'BSE' AND ticker = '920000'"
    ).fetchone() == (prior_id,)
    assert conn.execute(
        "SELECT count(*) FROM securities WHERE ticker IN ('000001', '600519')"
    ).fetchone()[0] == 0
    conn.rollback()


@pytest.mark.parametrize(
    "candidate",
    (
        sync.SecurityCandidate("HKEX", "000001", "错误市场"),
        sync.SecurityCandidate("SSE", "000001", "市场代码不符"),
        sync.SecurityCandidate("BSE", "92000", "短代码"),
        sync.SecurityCandidate(
            "SSE", "600519", "贵州茅台", (), "bad org id"
        ),
        sync.SecurityCandidate(
            "SSE",
            "600519",
            "贵州茅台",
            tuple(f"alias-{number}" for number in range(129)),
        ),
    ),
)
def test_sync_rejects_untrusted_candidate_objects_without_writes(conn, candidate):
    with pytest.raises(sync.SecurityMasterValidationError):
        sync.sync_confirmed_candidates(conn, [candidate])
    assert conn.execute("SELECT count(*) FROM securities").fetchone()[0] == 0
