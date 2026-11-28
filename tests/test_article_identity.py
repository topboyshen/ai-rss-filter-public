"""Contract tests for ``core.article_identity`` (the shared identity rules).

The golden values below were produced by the public pre-refactor implementation
(``db.database._normalized_article_identity``,
``db.database._safe_evidence_url`` and ``routes.webhook_routes._article_link``).
``event_identities`` stores these digests, so any drift would silently split
already-persisted events.  No network, no model calls.
"""

import hashlib
from datetime import datetime, timezone

import pytest

from core import article_identity as ai
from db import database, event_core

# link -> sha256 digest stored in event_identities (pre-refactor output)
GOLDEN_IDENTITY = {'https://www.cls.cn/telegraph': 'da8d5d3187dc4330d60cf4efe2ed7ca8edcd706d95c64c6d3f2446a406d0ff6a',
 'HTTPS://WWW.CLS.CN/telegraph': 'da8d5d3187dc4330d60cf4efe2ed7ca8edcd706d95c64c6d3f2446a406d0ff6a',
 'https://www.cls.cn:443/telegraph': 'da8d5d3187dc4330d60cf4efe2ed7ca8edcd706d95c64c6d3f2446a406d0ff6a',
 'http://example.com:80/a': '4e623f9326e486351e3d2b4448f0deeee7058eb31a9a48641121ec1450f32573',
 'http://example.com:8080/a': '4f8a0abea62380199435292a89e4bcd9b7906b303050a9283f324314dfa055fd',
 'https://example.com': 'b9b8633ecd7eeb8f25db73c03ecda2ddd2429c889ccbec8f7e3c8f1468904bb3',
 'https://example.com/a#frag': '91a324fc9e27ee186e0f00c548641878d14ed678abbfae7889d667f2e87a34d0',
 'https://example.com/a?b=1&c=2#x': 'e28c5ef66a9e1a6ac5774f927a73e6be956fb1273b401af825c6b893cb9abec5',
 'https://www.cls.cn/telegraph?ai_filter_item=h-abc': '7cc24280ed7e067c6fdfbcf682769088294b1ac369860349178003053c78b58d',
 '  https://example.com/a  ': '91a324fc9e27ee186e0f00c548641878d14ed678abbfae7889d667f2e87a34d0',
 'https://例子.测试/路径': 'f048ed654e26c64bbf4a52b107790be7a8710b5fada38e6fe0b89d72f2b864db',
 'https://[::1]:8443/x': '902c08c5aab1e640a78c1ac00df607efab04dab50b5c17c2f831c674b8b2b82e',
 'https://example.com:bad/x': '8abccd5a403eec1804a0ead1bdcfbf5b579d29a23b20c7524531bf303617f10d',
 'not a url': '54315ba21ed592eaf772b781b95d160c0d55351463981d09bc5061f5907841f0',
 'ftp://example.com/file': 'f7348a4b61b99334da3727806b396c0d7d7ff982d72c50197d627e413c2cc2ce',
 'https://user:pw@example.com/a': '91a324fc9e27ee186e0f00c548641878d14ed678abbfae7889d667f2e87a34d0',
 'https://example.com/A/B/': 'faf5b08bc5422c7155dfcc3da9e8c14e1e54b15e54429788115c9213867e9b31',
 'https://example.com/a?utm_source=x': '91a324fc9e27ee186e0f00c548641878d14ed678abbfae7889d667f2e87a34d0',
 'https://example.com/a?utm_medium=y&id=7': '49c55e7f19bfb83428fc69840a8a96cb89789bac6a90c3667a4634763e8fe292'}

GOLDEN_SAFE = {'https://www.cls.cn/telegraph': True,
 'HTTPS://WWW.CLS.CN/telegraph': True,
 'https://www.cls.cn:443/telegraph': True,
 'http://example.com:80/a': True,
 'http://example.com:8080/a': True,
 'https://example.com': True,
 'https://example.com/a#frag': True,
 'https://example.com/a?b=1&c=2#x': True,
 'https://www.cls.cn/telegraph?ai_filter_item=h-abc': True,
 '  https://example.com/a  ': True,
 'https://例子.测试/路径': True,
 'https://[::1]:8443/x': True,
 'https://example.com:bad/x': True,
 'not a url': False,
 'ftp://example.com/file': False,
 'https://user:pw@example.com/a': False,
 'https://example.com/A/B/': True}

# ((entry, url, title), per-item link) with the default shared section list
GOLDEN_LINKS = [(({'hash': 'abc123'}, 'https://www.cls.cn/telegraph', '标题一'),
  'https://www.cls.cn/telegraph?ai_filter_item=h-abc123'),
 (({'hash': '  '
            'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'},
   'https://www.cls.cn/telegraph/',
   '标题'),
  'https://www.cls.cn/telegraph?ai_filter_item=h-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'),
 (({'id': 42}, 'HTTPS://WWW.CLS.CN/telegraph', 't'),
  'https://www.cls.cn/telegraph?ai_filter_item=i-42'),
 (({'hash': True, 'id': 7}, 'https://www.cls.cn/telegraph', 't'),
  'https://www.cls.cn/telegraph?ai_filter_item=i-7'),
 (({}, 'https://www.cls.cn/telegraph', '  电报标题  '),
  'https://www.cls.cn/telegraph?ai_filter_item=t-f1cbc83dc0baebfc78150eba2d0d8553'),
 (({'hash': 'h'}, 'https://www.cls.cn/telegraph?x=1', 't'), 'https://www.cls.cn/telegraph?x=1'),
 (({'hash': 'h'}, 'https://www.cls.cn/telegraph#f', 't'), 'https://www.cls.cn/telegraph#f'),
 (({'hash': 'h'}, 'https://example.com/news/1', 't'), 'https://example.com/news/1'),
 (({'hash': 'h'}, 'http://www.cls.cn:80/telegraph', 't'),
  'https://www.cls.cn/telegraph?ai_filter_item=h-h'),
 (({'hash': 'h'}, 'https://www.cls.cn:8443/telegraph', 't'), 'https://www.cls.cn:8443/telegraph')]


@pytest.mark.parametrize("link,digest", sorted(GOLDEN_IDENTITY.items()))
def test_identity_digest_is_byte_identical_to_pre_refactor(link, digest):
    assert ai.article_identity_key(link) == ("article_url_sha256", digest)
    # The historic private entry point still answers the same.
    assert database._normalized_article_identity(link) == ("article_url_sha256", digest)


@pytest.mark.parametrize("link,expected", sorted(GOLDEN_SAFE.items()))
def test_safe_url_rule_is_unchanged(link, expected):
    assert ai.is_safe_article_url(link) is expected
    assert database._safe_evidence_url(link) is expected


@pytest.mark.parametrize("case,expected", GOLDEN_LINKS)
def test_per_item_link_is_unchanged(case, expected):
    entry, url, title = case
    assert ai.per_item_link(entry, url, title, ai.shared_section_urls({})) == expected


def test_empty_link_is_rejected():
    for empty in ("", "   ", None):
        with pytest.raises(ValueError):
            ai.article_identity_key(empty)


def test_shared_section_config_spellings_collapse_to_first_entry():
    shared = ai.shared_section_urls({"webhook_shared_section_urls": [
        "https://EXAMPLE.com/flash/", "http://example.com/flash", "https://x.test:8443/a",
    ]})
    assert shared == {'example.com/flash': 'https://example.com/flash', 'x.test:8443/a': 'https://x.test:8443/a'}
    assert ai.shared_section_urls({}) == {'www.cls.cn/telegraph': 'https://www.cls.cn/telegraph'}
    assert ai.shared_section_urls(None) == ai.shared_section_urls({})


@pytest.mark.parametrize("raw", [
    "", "  ", None, "\uf90a", " 销售\uf90a额 ", "e\u0301", "\u212b", "ＡＢＣ", "\u3000全角空格\u3000",
    "\t\n标题\r\n", "\ufb01", 12345,
])
def test_identity_text_matches_the_evidence_text_rule(raw):
    # #203: evidence hashes and identity keys must use the same canonical form.
    # db/event_core.py keeps its own copy because it must stay a standalone
    # stdlib-only file for the restore-drill bundle; this pins them together.
    assert ai.normalize_identity_text(raw) == event_core.normalize_evidence_text(raw)


def test_identity_text_is_nfc_not_nfkc():
    assert ai.normalize_identity_text(" \uf90a ") == "\u91d1"
    # NFKC would fold full-width letters; the evidence rule does not.
    assert ai.normalize_identity_text("ＡＢＣ") == "ＡＢＣ"
    assert ai.normalize_identity_text(None) == ""


def test_identity_module_imports_only_the_standard_library():
    import ast
    import inspect
    import sys

    tree = ast.parse(inspect.getsource(ai))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            roots.add(node.module.split(".")[0])
    roots.discard("__future__")
    assert roots <= set(sys.stdlib_module_names), roots


def test_title_fallback_key_is_nfc_stable():
    """Documented fix: without hash/id, the title fallback hashes the NFC form.

    Before the refactor U+F90A and U+91D1 (same visible glyph) produced two
    different per-item links, i.e. two events for one headline.
    """
    shared = ai.shared_section_urls({})
    compat = ai.per_item_link({}, "https://www.cls.cn/telegraph", "\uf90a价上涨", shared)
    plain = ai.per_item_link({}, "https://www.cls.cn/telegraph", "\u91d1价上涨", shared)
    assert compat == plain
    digest = hashlib.sha256("\u91d1价上涨".encode("utf-8")).hexdigest()[:32]
    assert plain == "https://www.cls.cn/telegraph?ai_filter_item=t-" + digest


def test_combined_entry_point_matches_the_two_runtime_steps():
    entry = {"hash": "abc"}
    url = "HTTPS://WWW.CLS.CN/telegraph/"
    result = ai.article_identity(url, entry=entry, title="t")
    expected_link = ai.per_item_link(entry, url, "t", ai.shared_section_urls(None))
    assert result.link == expected_link == "https://www.cls.cn/telegraph?ai_filter_item=h-abc"
    assert (result.identity_type, result.identity_key) == ai.article_identity_key(expected_link)
    # Without an upstream entry the link is taken as-is.
    plain = ai.article_identity("https://example.com/a#x")
    assert plain.link == "https://example.com/a#x"
    assert plain.identity_key == GOLDEN_IDENTITY["https://example.com/a#frag"]


def test_webhook_normalisation_uses_the_shared_rule(monkeypatch):
    from routes import webhook_routes

    calls = []
    real = ai.per_item_link

    def spy(entry, url, title, shared):
        calls.append(url)
        return real(entry, url, title, shared)

    monkeypatch.setattr(webhook_routes, "per_item_link", spy)
    out = webhook_routes._normalize_webhook_entries(
        [{"title": "x", "url": "https://www.cls.cn/telegraph", "hash": "h1"}], {}
    )
    assert calls == ["https://www.cls.cn/telegraph"]
    assert out[0]["url"] == "https://www.cls.cn/telegraph?ai_filter_item=h-h1"


def test_identity_module_is_outside_the_scoring_fingerprint():
    import inspect
    from core import config

    source = inspect.getsource(config._strategy_code_fingerprint)
    assert "article_identity" not in source


def test_webhook_normalisation_keeps_upstream_publish_time():
    from routes import webhook_routes

    out = webhook_routes._normalize_webhook_entries(
        [{
            "title": "x",
            "url": "https://example.test/a",
            "published_at": "2026-11-28T04:00:00Z",
        }],
        {},
    )
    assert out[0]["published_at"] == 1795838400

    missing = webhook_routes._normalize_webhook_entries(
        [{"title": "y", "url": "https://example.test/b"}], {}
    )
    assert "published_at" not in missing[0]


def test_webhook_entry_publish_time_reaches_the_processor():
    from core.processor import _entry_timestamp, _parsed_entry_timestamp

    # 2026-11-28 04:00:00 UTC, four hours before the 08:00 ingest.
    entry = {"published_at": 1795838400}
    wrapper = __import__(
        "routes.webhook_routes", fromlist=["_WebhookEntry"]
    )._WebhookEntry("t", "https://example.test/a", "s", published_at=entry["published_at"])
    assert _parsed_entry_timestamp(wrapper) == 1795838400
    assert _entry_timestamp(wrapper) == 1795838400


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, None),
        (None, None),
        ([], None),
        (1795838400, 1795838400),
        (1795838400.9, 1795838400),
        (datetime(2026, 11, 28, 4, tzinfo=timezone.utc), 1795838400),
        (datetime(2026, 11, 28, 4), 1795838400),
        ("1795838400", 1795838400),
        ("1795838400000", 1795838400),
        ("2026-11-28T04:00:00Z", 1795838400),
        ("2026-11-28T04:00:00", 1795838400),
        ("Sat, 28 Nov 2026 04:00:00 GMT", 1795838400),
        ("Sat, 28 Nov 2026 04:00:00", 1795838400),
        ("", None),
        ("not a date", None),
        (0, None),
        (-1, None),
        (float("nan"), None),
    ],
)
def test_coerce_entry_epoch_normalizes_supported_shapes(value, expected):
    from core.processor import _coerce_entry_epoch

    assert _coerce_entry_epoch(value) == expected
