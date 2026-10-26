"""Publisher identity equivalence and request-local bounded work checks."""

import copy
from concurrent.futures import ThreadPoolExecutor
from itertools import product
from threading import Barrier
from urllib.parse import unquote, urlsplit

import pytest

from services import event_daily_report as daily
from services import provenance_shadow as provenance


def _legacy_publisher_url(value):
    """Frozen validator before parsed publisher identities were reused."""
    raw = str(value or "").strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        return ""
    try:
        parts = urlsplit(raw)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None):
            return ""
        if any(segment in {".", ".."} for segment in unquote(parts.path).split("/")):
            return ""
        _ = parts.port
    except ValueError:
        return ""
    return raw


def _legacy_resolve(source_uri, source_name="", *, config=None):
    """Frozen resolver; unchanged host/trust primitives are borrowed only."""
    registry = provenance._normalize_registry(config)
    host = provenance._uri_host(source_uri)
    domain_matches = [
        (domain, metadata) for domain, metadata in registry["domains"].items()
        if host == domain or host.endswith("." + domain)
    ]
    if domain_matches:
        _match, metadata = max(domain_matches, key=lambda item: (len(item[0]), item[0]))
        group, source_class, scopes = metadata
        return dict(publisher_group=group, publisher_basis="registry_domain",
                    source_class=source_class, first_party_scopes=list(scopes))
    route_matches = []
    for path in provenance._trusted_route_candidates(source_uri, source_name):
        route_matches.extend(
            (route, metadata) for route, metadata in registry["routes"].items()
            if provenance._path_matches(path, route)
        )
    if route_matches:
        _match, metadata = max(route_matches, key=lambda item: (len(item[0]), item[0]))
        group, source_class, scopes = metadata
        return dict(publisher_group=group, publisher_basis="registry_route",
                    source_class=source_class, first_party_scopes=list(scopes))
    publisher_domain = provenance._registrable_domain(host)
    if publisher_domain:
        domain_group = f"domain:{publisher_domain}"
        if len(domain_group) > 128:
            domain_group = "domainhash:" + provenance._fingerprint({"domain": publisher_domain})
        return dict(publisher_group=domain_group, publisher_basis="registrable_domain",
                    source_class="unknown", first_party_scopes=[])
    return dict(publisher_group="unknown", publisher_basis="unknown",
                source_class="unknown", first_party_scopes=[])


def _legacy_outlet(item):
    identity = _legacy_resolve(
        _legacy_publisher_url(item["source_uri"]),
        _legacy_publisher_url(item["source_name"]), config=daily._PUBLISHER_REGISTRY,
    )
    key = str(identity["publisher_group"])
    return "" if key == "unknown" else key


def _legacy_summary(items):
    identified = [(_legacy_outlet(item), item["source_uri"]) for item in items]
    return (len({key for key, _url in identified} - {""}),
            len({url for key, url in identified if not key}))


REGISTRY = {"rules": [
    dict(publisher_group="base", domains=["example.com"], rss_routes=["/wire"],
         source_class="wire", first_party_scopes=["policy"]),
    dict(publisher_group="specific", domains=["press.example.com"],
         rss_routes=["/wire/detail"], source_class="official_record",
         first_party_scopes=["exchange", "issuer"]),
    dict(publisher_group="other-route", rss_routes=["/z/detail"]),
]}


def test_parsed_identity_matches_frozen_url_trust_and_longest_registry_rules(monkeypatch):
    monkeypatch.setattr(daily, "_PUBLISHER_REGISTRY", REGISTRY)
    long_domain = "a" * 63 + "." + "b" * 63
    uris = [
        "https://sub.press.example.com/a?route=/wire/detail",
        "https://example.com:443/wire/detail#fragment",
        "https://press.example.com.:65535/a", "https://unlisted.example/a",
        "https://news.company.com.cn/a", "https://例子.公司.cn/a",
        "https://" + long_domain + "/a", "http://127.0.0.1:1200/wire/detail/news",
        "http://rsshub//wire///detail?ignored=/z/detail",
        "http://[::1]:1200/wire", "http://192.168.1.7/wire",
        "http://[2001:4860:4860::8888]/wire", "https://evil.example/wire/detail",
        "https://rsshub.evil.com/wire/detail", "https://user:pass@example.com/a",
        "https://example.com:invalid/a", "https://example.com:65536/a",
        "https://example.com/%2e%2e/a", "https://example.com/a/%2E/b",
        "https://example.com/a\x00b", "https://example.com/\n/a", "https://[broken/",
        "https://bad_host.example/a", "javascript:alert(1)", "/wire/detail", "", None,
    ]
    sources = [
        "http://rsshub:1200/wire/detail/news", "http://localhost/z/detail",
        "http://[::1]/wire/detail", "http://192.168.1.7/wire",
        "https://evil.example/wire/detail", "http://rsshub.evil.com/wire/detail",
        "http://rsshub:65536/wire", "http://user:pass@rsshub/wire",
        "http://rsshub/%2e%2e/wire", "http://rsshub/wire?next=/z/detail",
        "/wire/detail", "普通媒体名称", "", None,
    ]
    registry = provenance._normalize_registry(REGISTRY)
    frozen_registry = copy.deepcopy(registry)
    rows = [dict(source_uri=uri, source_name=source) for uri, source in product(uris, sources)]
    expected = [_legacy_outlet(row) for row in rows]
    with daily._report_text_cache():
        assert [daily._outlet_key(row) for row in rows] == expected
        assert daily._publisher_summary(iter(rows)) == _legacy_summary(rows)
    assert [daily._outlet_key(row) for row in rows] == expected
    for uri, source in product(uris, sources):
        assert daily._publisher_url(uri) == _legacy_publisher_url(uri)
        assert provenance._resolve_publisher_with_registry(uri, source, registry=registry) == (
            _legacy_resolve(uri, source, config=REGISTRY)
        )
    assert registry == frozen_registry
    assert _legacy_outlet(rows[0]) == "specific"  # Domain wins over the feed route.


def test_unknown_links_remain_distinct_raw_links_not_identity_keys():
    rows = [
        dict(source_uri="http://rsshub:1200/unlisted?a=1", source_name=""),
        dict(source_uri="http://rsshub:1200/unlisted?a=2", source_name=""),
        dict(source_uri="http://rsshub:1200/unlisted?a=1", source_name="ordinary name"),
        dict(source_uri="https://bad_host/a", source_name=""),
        dict(source_uri="https://bad_host/a ", source_name=""),
        dict(source_uri="https://www.cls.cn/a", source_name=""),
        dict(source_uri="https://www.cls.cn/b", source_name=""),
    ]
    with daily._report_text_cache():
        assert daily._publisher_summary(iter(rows)) == _legacy_summary(rows) == (1, 4)


def test_fast_url_validator_preserves_all_control_positions_authorities_and_dot_segments():
    controls = [chr(value) for value in range(33)] + [chr(127), "\u00a0", "\u200b"]
    urls = []
    for control, host in product(controls, ["rsshub", "www.cls.cn", "[::1]", "bad_host"]):
        for position in ("prefix", "host", "path", "query", "suffix"):
            parts = dict(prefix="", host=host, path="/cls/telegraph", query="?q=1", suffix="")
            parts[position] = control + parts[position]
            urls.append(parts["prefix"] + "http://" + parts["host"]
                        + parts["path"] + parts["query"] + parts["suffix"])
    urls.extend("https://" + authority + path for authority, path in product([
        "example.com", "example.com:", "example.com:0", "example.com:65535",
        "example.com:65536", "example.com:invalid", "example.com:-1", "example.com: 80",
        "example.com:８０", "@example.com", "user@example.com", "user:@example.com",
        ":pass@example.com", "user%40name@example.com", "example.com%40other.example",
        "example.com：80", "[::1]", "[::1]:", "[::1]:1200", "[::1]:invalid",
    ], ["/plain/path", "/file.name", "/./path", "/../path", "/%2E/path", "/%2e%2e/path",
        "/prefix%2f..%2fsuffix", "/%252e/path", "/a.b?user=x@y", "/plain?port=:invalid"]))
    for url in urls:
        assert daily._publisher_url(url) == _legacy_publisher_url(url), repr(url)


def test_common_url_validation_does_not_repeat_authority_parsing_or_scan_in_python(monkeypatch):
    original = daily.urlsplit
    reads = {"hostname": 0, "username": 0, "password": 0, "port": 0}

    class Parsed:
        def __init__(self, url):
            self.parts = original(url)

        def __getattr__(self, name):
            if name in reads:
                reads[name] += 1
            return getattr(self.parts, name)

    def unexpected_character_loop(_char):
        raise AssertionError("whole URL control checks must not call Python once per character")

    monkeypatch.setattr(daily, "urlsplit", Parsed)
    monkeypatch.setattr(daily, "ord", unexpected_character_loop, raising=False)
    for index in range(100):
        url = f"https://news.example/article/{index}?q={index}"
        assert daily._publisher_url(url) == url
    assert reads == {"hostname": 100, "username": 0, "password": 0, "port": 0}
    assert daily._publisher_url("https://news.example:65536/article") == ""
    assert reads["port"] == 1
    assert daily._publisher_url("https://user@news.example/article") == ""
    assert daily._publisher_url("https://news.example/article\x00") == ""


def test_unique_article_urls_reuse_domain_feed_and_registry_work_with_one_total_bound(monkeypatch):
    calls = {"parse": 0, "host": 0, "registry": 0, "identity": 0}
    for name, key in [
        ("_publisher_url_parts", "parse"), ("_publisher_host_identity", "host"),
        ("_normalize_publisher_registry", "registry"), ("_resolve_publisher_identity", "identity"),
    ]:
        original = getattr(daily, name)

        def counted(*args, _key=key, _original=original, **kwargs):
            calls[_key] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(daily, name, counted)
    count = daily._TEXT_CACHE_SIZE * 2 + 19
    resolvers = []

    def rows():
        for index in range(count):
            yield dict(source_uri=f"https://news.example/a/{index}?article={index}",
                       source_name="http://rsshub:1200/cls/telegraph")
            resolver = daily._REPORT_PUBLISHER_STATE.get()[0]
            assert len(resolver.cache) <= daily._TEXT_CACHE_SIZE
        resolvers.append(resolver)

    with daily._report_text_cache():
        assert daily._publisher_summary(rows()) == (1, 0)
        assert len(resolvers[0].cache) == daily._TEXT_CACHE_SIZE
    assert calls == {"parse": count + 1, "host": 2, "registry": 1, "identity": 1}
    assert not resolvers[0].cache
    assert daily._REPORT_PUBLISHER_STATE.get() is None


def test_nested_failed_and_fresh_requests_release_only_their_own_identity_work(monkeypatch):
    row = dict(source_uri="https://news.example/a", source_name="")
    with daily._report_text_cache():
        outer_state = daily._REPORT_PUBLISHER_STATE.get()
        assert outer_state == [None]
        assert daily._outlet_key(row) == "domain:news.example"
        outer = outer_state[0]
        outer_entries = list(outer.cache.items())
        replacement = {"rules": [dict(publisher_group="replacement", domains=["news.example"])]}
        monkeypatch.setattr(daily, "_PUBLISHER_REGISTRY", replacement)
        with pytest.raises(RuntimeError, match="nested failure"):
            with daily._report_text_cache():
                assert daily._outlet_key(row) == "replacement"
                inner = daily._REPORT_PUBLISHER_STATE.get()[0]
                assert inner is not outer
                raise RuntimeError("nested failure")
        assert not inner.cache
        assert daily._REPORT_PUBLISHER_STATE.get() is outer_state
        assert list(outer.cache.items()) == outer_entries
        assert daily._outlet_key(row) == "domain:news.example"
    assert not outer.cache
    assert outer_state == []
    with daily._report_text_cache():
        assert daily._outlet_key(row) == "replacement"
        assert daily._REPORT_PUBLISHER_STATE.get()[0] is not outer


def test_registry_failures_and_identity_failures_do_not_escape_request_cleanup(monkeypatch):
    row = dict(source_uri="https://news.example/a", source_name="")
    with monkeypatch.context() as patch:
        patch.setattr(daily, "_PUBLISHER_REGISTRY", {"rules": "invalid"})
        with pytest.raises(provenance.ProvenanceShadowConfigError):
            with daily._report_text_cache():
                daily._outlet_key(row)
    assert daily._REPORT_PUBLISHER_STATE.get() is None
    captured = []

    def fail(*args, **kwargs):
        captured.append(daily._REPORT_PUBLISHER_STATE.get()[0])
        raise RuntimeError("identity failure")

    monkeypatch.setattr(daily, "_resolve_publisher_identity", fail)
    with pytest.raises(RuntimeError, match="identity failure"):
        with daily._report_text_cache():
            daily._outlet_key(row)
    assert not captured[0].cache
    assert daily._REPORT_PUBLISHER_STATE.get() is None


def test_simultaneous_requests_keep_identity_caches_separate():
    barrier = Barrier(2)

    def request():
        with daily._report_text_cache():
            row = dict(source_uri="https://news.example/a", source_name="")
            assert daily._outlet_key(row) == "domain:news.example"
            resolver = daily._REPORT_PUBLISHER_STATE.get()[0]
            barrier.wait(timeout=5)
            assert daily._outlet_key(row) == "domain:news.example"
            return resolver

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(request) for _ in range(2)]
        resolvers = [future.result(timeout=10) for future in futures]
    assert resolvers[0] is not resolvers[1]
    assert resolvers[0].registry is not resolvers[1].registry
    assert all(not resolver.cache for resolver in resolvers)


def test_card_and_sources_keep_frozen_publisher_output(monkeypatch):
    def row(index, uri, source=""):
        return dict(evidence_id=f"e{index}", source_uri=uri, source_name=source,
                    claim_text=f"测试公司公布项目进展{index}", stance="supports",
                    observed_at=100 + index, announcement_at=None)

    today = [row(1, "https://www.cls.cn/a"), row(2, "https://elsewhere.example/a"),
             row(3, "http://rsshub/unlisted?a=1"), row(4, "http://rsshub/unlisted?a=2")]
    history = [row(5, "https://www.cls.cn/b"),
               row(6, "http://rsshub/unlisted?a=1"),
               row(7, "https://article.example/a", "http://rsshub/wallstreetcn/live")]
    group = dict(title="测试公司公布项目进展", today=today, root_ids=["event"],
                 event_type="company", priority=7.5, associations=[])
    state = dict(block="advanced", history=history, fresh=today, denials=[],
                 corrections=[], prior_support=history, pending_language=False,
                 background_corrections=[])
    with monkeypatch.context() as patch:
        patch.setattr(daily, "_outlet_key", _legacy_outlet)
        expected = daily._item_view(group, state, frozenset(), period_label="本周")
    with daily._report_text_cache():
        assert daily._item_view(group, state, frozenset(), period_label="本周") == expected
