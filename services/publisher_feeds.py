"""Bounded publisher-native adapters for broken RSSHub routes.

No stale fallback, invented publication dates, model calls or database writes.
The configured feed identity stays unchanged. Fetches retain the shared pinned
DNS, redirect, size and TLS policy, using only fixed/validated publisher URLs.
"""
import json
import logging
import math
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from utils.helpers import fetch_feed, plain_text_from_markup

CSRC_INDEX = "https://www.csrc.gov.cn/csrc/xwfb/index.shtml"
CAIXIN_INDEX = "https://gateway.caixin.com/api/dataplatform/scroll/index"
WALLSTREET_API = "https://api-one.wallstcn.com/apiv1/content/"
_LOGGER = logging.getLogger(__name__)
_ROUTES = {"/gov/csrc/news": "csrc", "/caixin/latest": "caixin",
           "/wallstreetcn/news/global": "wallstreet"}
_RSSHUB_HOSTS = {"127.0.0.1", "localhost", "rsshub", "172.17.0.1"}
_ARTICLE_PATH = re.compile(r"/csrc/c[0-9]+/c[0-9]+/content\.shtml\Z")
_CAIXIN_ARTICLE_PATH = re.compile(r"/[0-9]{4}-[0-9]{2}-[0-9]{2}/[0-9]+\.html\Z")
_CAIXIN_TOPIC_PATH = re.compile(r"/topic/[A-Za-z0-9.]+\Z")
_CAIXIN_COLUMN_PATH = re.compile(r"/(?:column|special)/[A-Za-z0-9._-]+\Z")
_CAIXIN_AUTHORITY = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+caixin\.com(?::443)?\Z",
    re.IGNORECASE | re.ASCII,
)
_CAIXIN_MEDIA_HOSTS = frozenset({
    "fm.caixin.com", "video.caixin.com", "datanews.caixin.com",
})
_CAIXIN_TOPIC_HOST = "key.caixin.com"
_COVERAGE_FIELDS = ("source_unrecognized", "source_excluded_ads", "source_excluded_deleted")


def publisher_source_coverage(parsed_feed):
    """Read private adapter coverage counts without treating omissions as success."""
    metadata = getattr(parsed_feed, "feed", {})
    coverage = {}
    for field in _COVERAGE_FIELDS:
        value = metadata.get("ai_filter_" + field, "0")
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,9}", value):
            raise ValueError("Publisher source coverage metadata invalid")
        coverage[field] = int(value)
    return coverage


def _route(url):
    if not isinstance(url, str) or "\\" in url or any(ord(c) < 32 or ord(c) == 127 for c in url):
        return None
    try:
        p = urlsplit(url)
        if (p.scheme == "http" and p.hostname in _RSSHUB_HOSTS and p.port == 1200
                and p.username is None and p.password is None and not p.query and not p.fragment):
            return _ROUTES.get(p.path)
    except (ValueError, TypeError):
        pass
    return None


def _fetch(url, timeout, attempts):
    # Narrow independent policy, never inherit allow_unconfigured_feed_urls.
    return fetch_feed(url, timeout, max_attempts=attempts, cfg={"feeds": [{"url": url}]})


def _csrc_url(raw):
    url = urljoin(CSRC_INDEX, raw)
    p = urlsplit(url)
    if (p.scheme not in {"http", "https"} or p.hostname != "www.csrc.gov.cn"
            or p.port not in {None, 80, 443} or p.username is not None or p.password is not None
            or p.query or p.fragment or not _ARTICLE_PATH.fullmatch(p.path)):
        raise ValueError("CSRC article URL identity invalid")
    return "https://www.csrc.gov.cn" + p.path


class _CsrcPage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lists = []
        self.news = None
        self.anchor = None
        self.meta = {}
        self.body_depth = 0
        self.body = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            self.meta[attrs.get("name", "").lower()] = attrs.get("content", "")
        if tag == "ul":
            self.lists.append([])
        if tag == "a" and self.lists:
            self.anchor = [attrs.get("href", ""), []]
        if tag == "div":
            if self.body_depth:
                self.body_depth += 1
            elif "detail-news" in attrs.get("class", "").split():
                self.body_depth = 1
        if self.body_depth and tag in {"p", "br", "div"}:
            self.body.append(" ")

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor[1].append(data)
        if self.body_depth:
            self.body.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            href, pieces = self.anchor
            text = "".join(pieces).strip()
            if self.lists:
                if href == "/csrc/c100028/common_xq_list.shtml" and text == "加载更多":
                    if self.news is not None:
                        raise ValueError("CSRC news section ambiguous")
                    self.news = list(self.lists[-1])
                elif text:
                    self.lists[-1].append((href, text))
            self.anchor = None
        if tag == "ul" and self.lists:
            self.lists.pop()
        if tag == "div" and self.body_depth:
            self.body_depth -= 1


def _page(payload):
    page = _CsrcPage()
    page.feed(payload.decode("utf-8"))
    page.close()
    return page


def _csrc_article(url, timeout, attempts):
    page = _page(_fetch(url, timeout, attempts))
    title = page.meta.get("articletitle", "").strip()
    date = page.meta.get("pubdate", "")
    body = " ".join("".join(page.body).split())
    if not title or not body or not date:
        raise ValueError("CSRC article title/date/body missing")
    published = datetime.strptime(date, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone(timedelta(hours=8)))
    return {"title": title, "link": url, "description": body, "published": published}


def _csrc_items(limit, timeout, attempts):
    page = _page(_fetch(CSRC_INDEX, timeout, attempts))
    if not page.news:
        raise ValueError("CSRC current news section missing (not a valid empty feed)")
    links = list(dict.fromkeys(_csrc_url(href) for href, _ in page.news))
    if len(links) < limit:
        raise ValueError("CSRC index has fewer items than configured fetch limit")
    # Only configured items, not every article in the upstream page. Any failed
    # article fails this source; do not report partial reconstruction as success.
    with ThreadPoolExecutor(max_workers=3) as pool:
        return list(pool.map(lambda url: _csrc_article(url, timeout, attempts), links[:limit]))


def _caixin_link_class(url: str) -> str:
    """Classify one list URL. Named non-articles skip; conflicts still fail."""
    if (not isinstance(url, str) or not url or any(char in url for char in "\\?#")
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)):
        raise ValueError("Caixin article URL identity invalid")
    try:
        parsed = urlsplit(url)
    except ValueError:
        raise ValueError("Caixin article URL identity invalid") from None
    host = (parsed.hostname or "").lower()
    if (parsed.scheme != "https" or not _CAIXIN_AUTHORITY.fullmatch(parsed.netloc)
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment or parsed.query):
        raise ValueError("Caixin article URL identity invalid")
    path = parsed.path or ""
    if path.rsplit("/", 1)[-1] in {".", ".."}:
        raise ValueError("Caixin article URL identity invalid")
    if host in _CAIXIN_MEDIA_HOSTS and _CAIXIN_ARTICLE_PATH.fullmatch(path):
        return "media"
    if host == _CAIXIN_TOPIC_HOST and _CAIXIN_TOPIC_PATH.fullmatch(path):
        return "topic"
    if _CAIXIN_COLUMN_PATH.fullmatch(path):
        return "column"
    if host not in _CAIXIN_MEDIA_HOSTS and _CAIXIN_ARTICLE_PATH.fullmatch(path):
        return "article"
    raise ValueError("Caixin article URL identity invalid")


def _caixin_items(limit, timeout, attempts):
    payload = json.loads(_fetch(CAIXIN_INDEX, timeout, attempts))
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise ValueError("Caixin list response not successful")
    data = payload.get("data")
    rows = data.get("articleList") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("Caixin article list missing (not a valid empty feed)")
    items = []
    skipped = {"topic": 0, "column": 0, "media": 0}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Caixin article schema invalid")
        kind = _caixin_link_class(row.get("url", ""))
        if kind != "article":
            skipped[kind] += 1
            continue
        title, summary, stamp = row.get("title"), row.get("summary"), row.get("time")
        if not isinstance(title, str) or not title.strip() or not isinstance(summary, str):
            raise ValueError("Caixin title/summary missing")
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or stamp <= 0:
            raise ValueError("Caixin publication timestamp invalid")
        items.append({"title": plain_text_from_markup(title), "link": row["url"],
                      "description": plain_text_from_markup(summary),
                      "published": datetime.fromtimestamp(stamp / 1000, timezone.utc)})
        if len(items) >= limit:
            # Skip counts cover only rows traversed to fill the configured
            # article bound, not the unvisited remainder of the list.
            break
    skipped_total = sum(skipped.values())
    if skipped_total:
        _LOGGER.warning(
            "Caixin skipped %d recognized non-article links in traversed list prefix: %s",
            skipped_total,
            ", ".join(f"{name}={count}" for name, count in sorted(skipped.items()) if count),
        )
    if not items:
        raise ValueError(
            "Caixin list has no eligible articles "
            f"({skipped_total} recognized non-article links skipped)"
        )
    return items, skipped_total


def _wallstreet_data(url, timeout, attempts, *, allow_deleted=False):
    payload = json.loads(_fetch(url, timeout, attempts))
    if (allow_deleted and isinstance(payload, dict) and payload.get("code") == 60301
            and payload.get("message") == "内容不存在或已被删除" and payload.get("data") == {}):
        return None
    if (not isinstance(payload, dict) or payload.get("code") != 20000
            or not isinstance(payload.get("data"), dict)):
        raise ValueError("WallStreetCN response not successful")
    return payload["data"]


def _wallstreet_resource(row):
    kind, resource = row.get("resource_type"), row.get("resource")
    if kind not in ("article", "live") or not isinstance(resource, dict):
        raise ValueError("WallStreetCN resource schema invalid")
    identity, link, stamp = resource.get("id"), resource.get("uri"), resource.get("display_time")
    if isinstance(identity, bool) or not isinstance(identity, int) or identity <= 0:
        raise ValueError("WallStreetCN resource id invalid")
    if not isinstance(link, str) or "\\" in link or any(ord(c) < 33 or ord(c) == 127 for c in link):
        raise ValueError("WallStreetCN resource URL identity invalid")
    p = urlsplit(link)
    paths = ({f"/articles/{identity}", f"/member/articles/{identity}"}
             if kind == "article" else {f"/livenews/{identity}"})
    # The official global flow also labels chart/flash content as live. It is
    # served by the same lives/{id} endpoint, with a distinct public-page URI.
    if kind == "live" and resource.get("type") == "flash":
        paths.add(f"/charts/{identity}")
    if (p.scheme != "https" or p.hostname != "wallstreetcn.com" or p.port not in {None, 443}
            or p.username is not None or p.password is not None or p.query or p.fragment
            or p.path not in paths):
        raise ValueError("WallStreetCN resource URL identity invalid")
    if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
            or not math.isfinite(stamp) or stamp <= 0):
        raise ValueError("WallStreetCN publication timestamp invalid")
    try:
        published = datetime.fromtimestamp(stamp, timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("WallStreetCN publication timestamp invalid") from exc
    return kind, identity, link, published


def _wallstreet_article(resource, timeout, attempts):
    kind, identity, link, published = resource
    path = f"articles/{identity}?extract=0" if kind == "article" else f"lives/{identity}"
    data = _wallstreet_data(WALLSTREET_API + path, timeout, attempts, allow_deleted=True)
    if data is None:
        _LOGGER.warning("WallStreetCN publisher confirms deleted content: %s", link)
        return None
    detail_id = data.get("id")
    if isinstance(detail_id, bool) or not isinstance(detail_id, int) or detail_id != identity:
        raise ValueError("WallStreetCN detail id mismatch")
    if (kind == "live" and urlsplit(link).path == f"/charts/{identity}"
            and (data.get("type") != "flash" or data.get("uri") != link)):
        raise ValueError("WallStreetCN chart detail identity invalid")
    title = data.get("title") or (data.get("content_text") if kind == "live" else None)
    content, more = data.get("content"), data.get("content_more")
    if (not isinstance(title, str) or not title.strip() or not isinstance(content, str)
            or (more is not None and not isinstance(more, str))):
        raise ValueError("WallStreetCN title/body missing")
    title = plain_text_from_markup(title)
    body = plain_text_from_markup(content + (more or ""))
    if not title.strip() or not body.strip():
        raise ValueError("WallStreetCN title/body missing")
    return {"title": title, "link": link, "description": body, "published": published}


def _wallstreet_row_shape(row) -> str:
    """Bounded, URL-free description of a list row: ``type:/path/{n}``."""
    if not isinstance(row, dict):
        return f"row:{type(row).__name__}"
    kind = row.get("resource_type")
    kind = kind if isinstance(kind, str) and kind.isascii() and len(kind) <= 16 else "?"
    resource = row.get("resource")
    uri = resource.get("uri") if isinstance(resource, dict) else None
    if not isinstance(uri, str):
        return f"{kind}:<no-uri>"
    try:
        path = urlsplit(uri.strip()).path
    except ValueError:
        return f"{kind}:<unparsable>"
    path = re.sub(r"[0-9]+", "{n}", path)
    path = "".join(c if c.isascii() and c.isprintable() else "?" for c in path)[:48]
    return f"{kind}:{path or '/'}"


def _wallstreet_items(limit, timeout, attempts):
    url = WALLSTREET_API + f"information-flow?channel=global-channel&accept=article&limit={limit}"
    rows = _wallstreet_data(url, timeout, attempts).get("items")
    if not isinstance(rows, list) or not rows:
        raise ValueError("WallStreetCN list missing (not a valid empty feed)")
    selected, skipped, last_error, excluded_ads = [], {}, None, 0
    for row in rows:
        if isinstance(row, dict) and row.get("resource_type") == "ad":
            excluded_ads += 1
            continue
        try:
            if not isinstance(row, dict):
                raise ValueError("WallStreetCN list row invalid")
            selected.append(_wallstreet_resource(row))
        except ValueError as exc:
            # One unrecognized row is skipped, never served; only its type and
            # path shape are logged.  Detail failures below still fail closed.
            last_error = exc
            shape = _wallstreet_row_shape(row)
            skipped[shape] = skipped.get(shape, 0) + 1
            continue
        if len(selected) >= limit:
            break  # The publisher may ignore limit; never fetch its whole list.
    if skipped:
        _LOGGER.warning(
            "WallStreetCN skipped %d unrecognized list rows: %s",
            sum(skipped.values()),
            ", ".join(f"{shape}x{count}" for shape, count in sorted(skipped.items())),
        )
    if not selected and last_error is not None:
        raise ValueError(
            f"WallStreetCN list has no recognizable rows ({sum(skipped.values())} "
            f"skipped; last: {last_error})"
        )
    if not selected:
        raise ValueError("WallStreetCN list has no eligible articles")
    # A failed selected detail fails the source rather than clearing degradation
    # with a partial feed, cached placeholder or invented list-summary fallback.
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda row: _wallstreet_article(row, timeout, attempts), selected))
    items = [item for item in results if item is not None]
    if not items:
        raise ValueError("WallStreetCN selected list has no eligible public articles")
    return items, {
        "source_unrecognized": sum(skipped.values()),
        "source_excluded_ads": excluded_ads,
        "source_excluded_deleted": len(results) - len(items),
    }


def _rss(source, items, coverage=None, note=""):
    root = ET.Element("rss", version="2.0")
    channel = ET.SubElement(root, "channel")
    metadata = {
        "csrc": ("证监会新闻", CSRC_INDEX, "发布方原始正文"),
        "caixin": ("财新网", "https://www.caixin.com/", "发布方公开列表摘要（非全文）"),
        "wallstreet": ("华尔街见闻全球", "https://wallstreetcn.com/news/global",
                       "发布方接口公开正文／摘录（不绕过付费权限）"),
    }
    if source == "wallstreet":
        coverage = coverage or dict.fromkeys(_COVERAGE_FIELDS, 0)
        title, link, description = metadata[source]
        metadata[source] = (
            title, link, description + f"；{len(items)}条公开内容，"
            f"{coverage['source_excluded_deleted']}条已删除未展示，"
            f"{coverage['source_unrecognized']}条未识别（采集缺口）",
        )
    title, link, description = metadata[source]
    if note:
        description += note
    for key, value in zip(("title", "link", "description"), (title, link, description)):
        ET.SubElement(channel, key).text = value
    if coverage is not None:
        for field in _COVERAGE_FIELDS:
            ET.SubElement(channel, "ai_filter_" + field).text = str(coverage[field])
    for entry in items:
        item = ET.SubElement(channel, "item")
        for key in ("title", "link", "description"):
            ET.SubElement(item, key).text = entry[key]
        ET.SubElement(item, "guid", isPermaLink="true").text = entry["link"]
        ET.SubElement(item, "pubDate").text = format_datetime(entry["published"])
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def load_publisher_feed(target_url, cfg, timeout, attempts):
    """Return None for unrelated routes; refuse unconfigured adapter requests."""
    source = _route(target_url)
    if source is None:
        return None
    feeds = [feed for feed in cfg.get("feeds", []) if isinstance(feed, dict)
             and _route(feed.get("url", "")) == source and feed.get("enabled", True)]
    if len(feeds) != 1:
        raise PermissionError("Publisher adapter requires one enabled configured source")
    limit = int(feeds[0].get("max_entries") or cfg.get("max_entries", 15))
    if not 1 <= limit <= 50:
        raise ValueError("Publisher fetch limit must be 1..50")
    adapters = {"csrc": _csrc_items, "caixin": _caixin_items, "wallstreet": _wallstreet_items}
    items = adapters[source](limit, timeout, attempts)
    if source == "wallstreet":
        items, coverage = items
        return _rss(source, items, coverage)
    if source == "caixin":
        items, skipped = items
        note = f"；本次已遍历范围：已识别非文章链接 {skipped} 条未采集" if skipped else ""
        return _rss(source, items, note=note)
    return _rss(source, items)
