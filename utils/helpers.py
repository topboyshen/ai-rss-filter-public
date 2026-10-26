import re
import time
import threading
import email.utils
import html
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from typing import Optional, Dict, Any
from urllib.request import Request, ProxyHandler, build_opener


# 预编译正则（避免重复编译）
TITLE_CLEAN_RE = re.compile(
    r'^\[\u2b50\d+(?:\.\d+)?(?:\u5206)?[^\]]*\]\s*'
)
LOGIC_CLEAN_RE = re.compile(r'\s*\U0001f4a1.*$')
NOISE_CLEAN_RE = re.compile(r'(重点[!！]|突发[!！]|快讯[:：]|解读[:：]|[!！]重点|[!！]突发)')
SCORE_RE = re.compile(r'\u2b50(\d+(?:\.\d+)?)')
# 标签提取：优先旧格式 🌊tag，再试新格式 📈📉⚖️ tag
# 用顺序控制优先级：先匹配 🌊，失败则匹配其他情绪图标
TAG_RE_WAVE = re.compile(r'\U0001f30a\s*([^\]|]+)')
TAG_RE_EMOJI = re.compile(r'(?:\U0001f4c8|\U0001f4c9|\u2696\ufe0f)\s*([^\]|]+)')
DISPLAY_HIGHLIGHT_TAG_RE = re.compile(
    r'</?(?:em|strong|b|i|mark)\b[^>]*>',
    re.IGNORECASE,
)
_EMBEDDED_CODE_BLOCK_RE = re.compile(
    r'<\s*(script|style)\b[^>]{0,2048}>.*?<\s*/\s*\1\s*>',
    re.IGNORECASE | re.DOTALL,
)
_HTML_BREAK_TAG_RE = re.compile(
    r'<\s*(?:br|hr)\b[^<>]{0,1024}/?\s*>'
    r'|<\s*/\s*(?:address|article|aside|blockquote|div|figcaption|figure|footer|'
    r'h[1-6]|header|li|main|nav|ol|p|section|table|tbody|td|tfoot|th|thead|tr|ul)\s*>',
    re.IGNORECASE,
)
_KNOWN_HTML_TAG_NAMES = (
    r'a|abbr|address|article|aside|audio|b|blockquote|body|br|button|'
    r'caption|cite|code|col|colgroup|data|datalist|dd|del|details|dfn|dialog|div|'
    r'dl|dt|em|embed|fieldset|figcaption|figure|font|footer|form|h[1-6]|head|'
    r'header|hgroup|hr|html|i|iframe|img|input|ins|kbd|label|legend|li|link|main|'
    r'map|mark|menu|meta|meter|nav|noscript|object|ol|optgroup|option|output|p|'
    r'picture|pre|progress|q|rp|rt|ruby|s|samp|script|search|section|select|slot|'
    r'small|source|span|strong|style|sub|summary|sup|table|tbody|td|template|'
    r'textarea|tfoot|th|thead|time|title|tr|track|u|ul|var|video|wbr'
)
_KNOWN_HTML_TAG_RE = re.compile(
    rf'<\s*/?\s*(?:{_KNOWN_HTML_TAG_NAMES})\b'
    r'[^<>]{0,2048}/?\s*>',
    re.IGNORECASE,
)
_TRAILING_TRUNCATED_KNOWN_HTML_TAG_RE = re.compile(
    # A known tag whose closing ``>`` never arrived.  Once such a fragment
    # starts, the remainder belongs to that malformed tag (including text a
    # downstream formatter may have appended after it).
    rf'<\s*/?\s*(?:{_KNOWN_HTML_TAG_NAMES})\b(?=[^>]*\Z)[\s\S]*\Z',
    re.IGNORECASE,
)


def plain_text_from_markup(value: object) -> str:
    """Return readable text after conservatively removing known HTML markup.

    Some RSS providers put an HTML list in ``title`` and may XML-escape it one
    or two times.  Decode at most two layers, remove executable blocks, turn
    structural breaks into spaces, and strip only known HTML tags.  This keeps
    ordinary market text such as ``研发投入<5%`` and ``1 < 2`` intact.

    This is an input-normalization helper, not an output escaping boundary;
    every HTML renderer must still escape the returned text for its context.
    """
    text = str(value or "")
    if not text:
        return ""
    for _ in range(2):
        decoded = html.unescape(text)
        text = decoded
        if "&" not in text:
            break
    text = _EMBEDDED_CODE_BLOCK_RE.sub(" ", text)
    text = _HTML_BREAK_TAG_RE.sub(" ", text)
    # Inline highlight tags must not split words (``<em>A</em>股`` → ``A股``).
    # Structural tags were already converted to spaces immediately above.
    text = _KNOWN_HTML_TAG_RE.sub("", text)
    text = _TRAILING_TRUNCATED_KNOWN_HTML_TAG_RE.sub("", text)
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def feed_data_health(row, cfg: Optional[dict] = None) -> Dict[str, Any]:
    """Return data-freshness state while keeping transport health separate."""
    cfg = cfg or {}
    health_cfg = cfg.get("feed_health", {}) or {}
    if not isinstance(health_cfg, dict):
        health_cfg = {}
    try:
        degraded_after = max(
            1,
            int(health_cfg.get("empty_degraded_after", 3) or 3),
        )
    except (TypeError, ValueError):
        degraded_after = 3
    try:
        empty_checks = max(0, int(row[10] or 0)) if len(row) > 10 else 0
    except (TypeError, ValueError):
        empty_checks = 0
    try:
        last_nonempty_at = max(0, int(row[11] or 0)) if len(row) > 11 else 0
    except (TypeError, ValueError):
        last_nonempty_at = 0
    try:
        last_check = max(0, int(row[2] or 0)) if len(row) > 2 else 0
        total_checks = (
            max(0, int(row[4] or 0)) + max(0, int(row[5] or 0))
            if len(row) > 5 else 0
        )
    except (TypeError, ValueError):
        last_check = 0
        total_checks = 0

    if empty_checks >= degraded_after:
        state = "stale_empty"
        label = "连续空源"
        degraded = True
    elif empty_checks > 0:
        state = "transient_empty"
        label = "暂时为空"
        degraded = False
    elif last_check <= 0 and total_checks <= 0:
        state = "unknown"
        label = "未检查"
        degraded = False
    else:
        state = "healthy"
        label = "正常"
        degraded = False
    return {
        "data_state": state,
        "data_state_label": label,
        "data_degraded": degraded,
        "consecutive_empty_checks": empty_checks,
        "last_nonempty_at": last_nonempty_at,
        "empty_degraded_after": degraded_after,
    }


class MemoryCache:
    """线程安全的内存缓存（替代全局 CACHE）"""

    def __init__(self, max_size: int = 50):
        self._store: Dict[str, tuple] = {}
        self._lock = threading.Lock()
        self._max_size = max_size

    def get(self, url: str, ttl: int) -> Optional[str]:
        with self._lock:
            if url not in self._store:
                return None
            ts, content = self._store[url]
            if time.time() - ts >= ttl:
                del self._store[url]
                return None
            return content

    def set(self, url: str, content: str) -> None:
        with self._lock:
            self._store[url] = (time.time(), content)
            if len(self._store) > self._max_size:
                oldest = min(self._store.keys(), key=lambda k: self._store[k][0])
                del self._store[oldest]


class StatsBuffer:
    """线程安全的统计缓冲（替代全局 _stats_buffer）"""

    def __init__(self, flush_interval: int = 60, flush_threshold: int = 20):
        self._buffer: Dict[str, int] = {
            "total_requests": 0, "api_calls_made": 0, "cache_hits": 0,
            "similarity_hits": 0, "blacklist_hits": 0, "fast_pass_hits": 0,
            "filtered_count": 0, "passed_count": 0
        }
        self._lock = threading.Lock()
        self._last_flush = time.time()
        self._flush_interval = flush_interval
        self._flush_threshold = flush_threshold

    def bump(self, db_update_stats, **kwargs) -> None:
        with self._lock:
            for k, v in kwargs.items():
                self._buffer[k] = self._buffer.get(k, 0) + v
            if (time.time() - self._last_flush > self._flush_interval or
                    sum(self._buffer.values()) > self._flush_threshold):
                db_update_stats(self._buffer)
                self._buffer = {k: 0 for k in self._buffer}
                self._last_flush = time.time()

    def flush(self, db_update_stats) -> None:
        with self._lock:
            if sum(self._buffer.values()) > 0:
                db_update_stats(self._buffer)
                self._buffer = {k: 0 for k in self._buffer}
                self._last_flush = time.time()


# 全局实例（单例，但封装在类内）
_cache = MemoryCache()
_stats = StatsBuffer()


def strategy_cache_key(key: str, cfg: dict) -> str:
    version = str(cfg.get("scoring_strategy_version") or "")
    return f"{key}#strategy={version}" if version else key


def get_cached(url: str, cache_ttl: int) -> Optional[str]:
    return _cache.get(url, cache_ttl)


def set_cached(url: str, content: str) -> None:
    _cache.set(url, content)


def bump_stat(db_update_stats, **kwargs) -> None:
    _stats.bump(db_update_stats, **kwargs)


def flush_stats(db_update_stats) -> None:
    _stats.flush(db_update_stats)


class FeedEntry:
    """与 feedparser 条目共用的轻量对象，供 build_rss_xml / 多源合并使用"""

    __slots__ = (
        "title",
        "link",
        "ai_summary",
        "published_parsed",
        "_raw_title",
        "scoring_meta",
    )

    def __init__(
        self,
        title: str,
        link: str,
        summary: str = "",
        published_parsed: Any = None,
    ):
        self.title = title
        self.link = link
        self.ai_summary = summary
        self.published_parsed = published_parsed
        self._raw_title = title
        self.scoring_meta = {}

    def get(self, key: str, default: str = ""):
        return getattr(self, key, default)


def resolve_feed_url_to_cache_key(target_url: str, cfg: dict) -> str:
    """Map only exact configuration-generated aliases to a refresh cache key."""
    for feed in cfg.get("feeds", []) or []:
        feed_url = str(feed.get("url") or "")
        if feed_url == target_url:
            return feed_url
    aliases = cfg.get("_runtime_feed_url_aliases", {}) or {}
    if isinstance(aliases, dict):
        for canonical, originals in aliases.items():
            family = [str(canonical or "")]
            if isinstance(originals, (list, tuple)):
                family.extend(str(item or "") for item in originals)
            if target_url in family:
                return str(canonical or target_url)
    return target_url


def max_entries_for_feed_url(target_url: str, cfg: dict) -> Optional[int]:
    """若 feeds[] 中为该 URL 配置了 max_entries，则覆盖全局（减轻高频源 LLM 压力）。"""
    canonical = resolve_feed_url_to_cache_key(target_url, cfg)
    for feed in cfg.get("feeds", []):
        feed_url = (feed.get("url") or "").strip()
        if not feed_url or feed_url != canonical:
            continue
        v = feed.get("max_entries")
        if v is None:
            return None
        try:
            return max(1, int(v))
        except (TypeError, ValueError):
            return None
    return None


# ===== 相似度引擎 =====
# 常见新闻来源前缀（在相似度比较前移除，避免格式化标题导致的误判）
def _strip_source_prefixes(text: str) -> str:
    # 财联社电报: "财联社4月24日电，"
    text = re.sub(r'财联社\d+月\d+日电[，,：:]', '', text)
    # 华尔街见闻
    text = re.sub(r'华尔街见闻[：:]', '', text)
    # 36氪快讯
    text = re.sub(r'36氪[快讯日报][：:]', '', text)
    # 雷锋网/其他来源的日期前缀
    text = re.sub(r'\d+月\d+日[，,：:]', '', text)
    # Reddit 常见前缀
    text = re.sub(r'^\s*💬\s*', '', text)
    return text.strip()


def format_article_display_title(title: str, raw_title: Optional[str] = None) -> str:
    """展示用标题：去掉模型加的 [⭐…] 前缀与尾部 💡 点评，与 dashboard / digest / glance 一致。"""
    t = (raw_title if raw_title is not None else title) or ""
    t = t.strip()
    if not t:
        t = (title or "").strip()
    if t.startswith("[⭐"):
        t = t.split("]", 2)[-1].strip()
        if "💡" in t:
            t = t.split("💡")[0].strip()
    # 搜索高亮和部分快讯源会把 HTML 列表放入标题。输入边界先转为
    # 纯文本；各渲染器仍需按自己的 HTML/XML 上下文再次转义。
    t = plain_text_from_markup(t)
    # Never restore the scored title after normalization removed a markup-only
    # value.  Output callers rely on this helper as a fail-closed display
    # boundary and can supply their own explicit empty-title placeholder.
    return t


_DIRECTION_NEGATION_RE = re.compile(
    r"(?:不再|并非|并不|不是|未必|不宜|没有|否认|不)\s*(看多|看空)"
)
_DIRECTION_TRANSITION_MARKERS = ("转为", "转向", "改为", "变为", "转而")


def normalize_direction(value: object = "", *, text: object = "") -> str:
    """Return a canonical 看多/看空/中性 value.

    ``value`` is the structured scorer output and therefore wins when it is an
    exact direction or emoji. ``text`` is only a compatibility fallback for old
    cache rows. Negated statements do not become signals, while an explicit
    transition uses the direction after the transition marker.
    """
    raw_value = str(value or "").strip()
    exact = {
        "📈": "看多",
        "看多": "看多",
        "多": "看多",
        "bullish": "看多",
        "📉": "看空",
        "看空": "看空",
        "空": "看空",
        "bearish": "看空",
        "⚖️": "中性",
        "⚡": "中性",
        "中性": "中性",
        "neutral": "中性",
    }
    canonical = exact.get(raw_value.lower()) or exact.get(raw_value)
    if canonical:
        return canonical

    candidate = " ".join(part for part in (raw_value, str(text or "")) if part).strip()
    if not candidate:
        return "中性"

    transition_at = max((candidate.rfind(marker) for marker in _DIRECTION_TRANSITION_MARKERS), default=-1)
    if transition_at >= 0:
        tail = candidate[transition_at:]
        normalized_tail = _DIRECTION_NEGATION_RE.sub("", tail)
        tokens = [
            (normalized_tail.rfind("看多"), "看多"),
            (normalized_tail.rfind("📈"), "看多"),
            (normalized_tail.rfind("看空"), "看空"),
            (normalized_tail.rfind("📉"), "看空"),
        ]
        position, direction = max(tokens, key=lambda item: item[0])
        if position >= 0:
            return direction
        if normalized_tail != tail:
            return "中性"

    candidate = _DIRECTION_NEGATION_RE.sub("", candidate)
    has_bull = "📈" in candidate or "看多" in candidate
    has_bear = "📉" in candidate or "看空" in candidate
    if has_bull == has_bear:
        return "中性"
    return "看多" if has_bull else "看空"


def direction_emoji(direction: object) -> str:
    return {"看多": "📈", "看空": "📉"}.get(normalize_direction(direction), "⚖️")


def clean_title_for_compare(title: str) -> str:
    t = TITLE_CLEAN_RE.sub('', title)
    t = LOGIC_CLEAN_RE.sub('', t)
    t = NOISE_CLEAN_RE.sub('', t)
    t = _strip_source_prefixes(t)
    return t.strip()


def title_similarity(a: str, b: str) -> float:
    ca = clean_title_for_compare(a)
    cb = clean_title_for_compare(b)
    if not ca or not cb:
        return 0.0
    return SequenceMatcher(None, ca, cb).ratio()


def find_similar_article(title: str, entries: list, threshold: float):
    best_match = None
    best_score = 0.0
    for row in entries:
        link, cached_title, raw_title, score, tag, logic = row[:6]
        compare_title = raw_title if raw_title else cached_title
        sim = title_similarity(title, compare_title)
        if sim > best_score:
            best_score = sim
            # New DB rows carry structured direction in position 6.  Keep the
            # six-column legacy shape intact so callers can distinguish old
            # rows that still require title-text inference.
            best_match = tuple(row[:7]) if len(row) > 6 else tuple(row[:6])
    if best_score >= threshold:
        return best_match, best_score
    return None, best_score


# ===== 工具函数 =====
def rfc822_date(ts: Optional[float] = None) -> str:
    if ts is None:
        ts = time.time()
    return email.utils.formatdate(ts, localtime=False, usegmt=True)


def escape_html(value: Any) -> str:
    """Escape untrusted data for an HTML text or quoted attribute context."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def safe_http_url(value: Any, fallback: str = "#") -> str:
    """Return an absolute HTTP(S) URL or a non-executable fallback.

    HTML escaping alone does not make a URL safe: ``javascript:`` remains
    executable after entity encoding. Reject control characters, non-HTTP
    schemes, relative URLs, and malformed host/port combinations before the
    caller places the result in an ``href`` attribute.
    """
    raw = "" if value is None else str(value).strip()
    if not raw or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        return fallback
    try:
        parsed = urllib.parse.urlsplit(raw)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return fallback
        # Accessing port validates malformed values such as ``:not-a-port``.
        _ = parsed.port
    except (TypeError, ValueError):
        return fallback
    return raw


def escape_xml(value: Any) -> str:
    """Escape untrusted XML text, including quotes for attribute-safe reuse."""
    return escape_html(value)


def escape_cdata(value: Any) -> str:
    """Split the forbidden ``]]>`` terminator across adjacent CDATA blocks."""
    text = "" if value is None else str(value)
    return text.replace("]]>", "]]]]><![CDATA[>")


def highlight_title(title: str, keywords: list) -> str:
    """Highlight keywords safely; escape the title before injecting markup."""
    if not keywords:
        return escape_html(title)
    result = escape_html(title)
    for kw in keywords:
        safe_kw = escape_html(kw)
        if safe_kw and safe_kw in result and f"<b>{safe_kw}</b>" not in result:
            result = result.replace(safe_kw, f"<b>{safe_kw}</b>")
    return result


def extract_score_from_title(title: str) -> float:
    m = SCORE_RE.search(title)
    return float(m.group(1)) if m else 0.0


def extract_tag_from_title(title: str) -> Optional[str]:
    # 优先匹配旧格式 🌊tag，再匹配新格式 📈📉⚖️ tag
    m = TAG_RE_WAVE.search(title) or TAG_RE_EMOJI.search(title)
    return m.group(1).strip() if m else None


def fetch_feed(
    target_url: str,
    timeout: int,
    max_attempts: int = 3,
    backoff_base: float = 2.0,
    cfg: Optional[dict] = None,
) -> bytes:
    """拉取 RSS 字节。失败时按 max_attempts 重试（超时、连接错误等）。

    Initial and redirect targets use the single policy in ``routes.security``.
    A missing config is intentionally fail-closed.
    """
    import os
    import time as _time
    from routes.security import (
        PinnedHTTPHandler,
        PinnedHTTPSHandler,
        ValidatingRedirectHandler,
        pin_request_target,
    )

    if max_attempts < 1:
        max_attempts = 1
    # 修复 URL 中的空格（Flask 解析查询参数时 + 变空格导致）
    target_url = target_url.replace(' ', '%20')
    policy_cfg = cfg or {}
    try:
        configured_max_bytes = int(
            os.environ.get("AI_FILTER_MAX_FEED_BYTES", 8 * 1024 * 1024)
        )
    except (TypeError, ValueError):
        configured_max_bytes = 8 * 1024 * 1024
    max_feed_bytes = max(
        64 * 1024,
        min(32 * 1024 * 1024, configured_max_bytes),
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    def _do_fetch():
        # An environment proxy would perform a fresh, untrusted DNS lookup and
        # defeat address pinning. Feed fetches therefore always use a direct
        # connection through the pinned handlers.
        handlers = [
            ProxyHandler({}),
            PinnedHTTPHandler(policy_cfg),
            PinnedHTTPSHandler(policy_cfg),
            ValidatingRedirectHandler(policy_cfg),
        ]
        opener = build_opener(*handlers)
        req = Request(target_url, headers=headers)
        pin_request_target(req, policy_cfg)
        with opener.open(req, timeout=timeout) as resp:
            payload = resp.read(max_feed_bytes + 1)
            if len(payload) > max_feed_bytes:
                raise ValueError(
                    f"feed response exceeds {max_feed_bytes} byte limit"
                )
            return payload

    last_err: Optional[Exception] = None
    for attempt in range(max_attempts):
        try:
            return _do_fetch()
        except PermissionError:
            # Policy failures cannot become valid through retry/backoff.
            raise
        except Exception as e:
            last_err = e
            if attempt == max_attempts - 1:
                break
            _time.sleep(backoff_base * (attempt + 1))
    if last_err:
        raise last_err
    raise RuntimeError("fetch_feed: no attempts")

def clean_html(html_text: str) -> str:
    """移除 HTML 标签和实体，返回纯文本；对严重污染的 HTML 返回空字符串"""
    if not html_text:
        return ""
    # 1. 移除所有 HTML 标签
    text = re.sub(r'<[^>]*>', ' ', html_text)
    # 2. 移除属性键值对
    text = re.sub(r'\w+\s*=\s*"[^"]*"', ' ', text)
    text = re.sub(r"\w+\s*=\s*'[^']*'", ' ', text)
    # 3. 解码实体
    text = text.replace('&nbsp;', ' ').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&amp;', '&').replace('&quot;', '"').replace('&apos;', "'")
    # 4. 移除常见 HTML/CSS/JS 关键词残留（而非所有英文）
    html_keywords = re.compile(
        r'\b(?:div|span|p|br|hr|img|a|href|src|alt|class|style|id|script|'
        r'function|var|let|const|return|if|else|for|while|true|false|null|'
        r'window|document|body|head|html|meta|link|table|tr|td|th|ul|ol|li)\b',
        re.IGNORECASE
    )
    text = html_keywords.sub(' ', text)
    # 5. 移除 URL 残留
    text = re.sub(r'https?://[^\s]*', ' ', text)
    # 6. 移除孤立标点和符号
    text = re.sub(r'\s*[=\\/]+\s*', ' ', text)
    text = re.sub(r'\s*[\"\'\'>]+\s*', ' ', text)
    # 7. 合并空白
    text = re.sub(r'\s+', ' ', text).strip()
    # 8. 如果有效中文内容太少（<10%），返回空
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
    if len(text) > 20 and chinese_chars / len(text) < 0.1:
        return ""
    return text
