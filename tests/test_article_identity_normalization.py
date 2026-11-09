"""Article identity must ignore presentation-only URL variants.

The same story reaches the receiver through different transports: RSSHub
appends ``utm_*`` parameters, Miniflux appends ``from=``, and publishers flip
path case or add a trailing slash.  Those variants must resolve to one durable
observation key, while genuinely different items, and query parameters that
carry item identity, must stay distinct.
"""

from __future__ import annotations

import pytest

from db import database

BASE = "https://example.com/news/2026/11/08/story-1"


def _key(link: str) -> tuple[str, str]:
    return database._normalized_article_identity(link)


@pytest.mark.parametrize(
    "variant",
    [
        BASE + "?utm_source=rss&utm_medium=feed",
        BASE + "?UTM_Campaign=Morning",
        BASE + "?from=miniflux",
        "https://example.com/News/2026/11/08/Story-1",
        BASE + "/",
        BASE + "#section-2",
        "https://EXAMPLE.com:443/news/2026/11/08/story-1",
    ],
)
def test_presentation_variants_share_one_identity(variant: str) -> None:
    assert _key(variant) == _key(BASE)


@pytest.mark.parametrize(
    "variant",
    [
        BASE + "?id=2",
        BASE + "?ai_filter_item=h-abc",
        "https://example.com/news/2026/11/08/story-2",
        "http://example.com/news/2026/11/08/story-1",
    ],
)
def test_distinct_articles_keep_distinct_identities(variant: str) -> None:
    assert _key(variant) != _key(BASE)
