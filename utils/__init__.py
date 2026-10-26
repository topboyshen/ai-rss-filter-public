"""
AI-Filter 工具函数包

提供缓存、相似度计算、RSS 工具等通用功能。
"""

from .helpers import (
    MemoryCache,
    StatsBuffer,
    get_cached,
    set_cached,
    bump_stat,
    flush_stats,
    clean_title_for_compare,
    format_article_display_title,
    title_similarity,
    find_similar_article,
    rfc822_date,
    escape_xml,
    highlight_title,
    extract_score_from_title,
    extract_tag_from_title,
    fetch_feed,
)

__all__ = [
    "MemoryCache",
    "StatsBuffer",
    "get_cached",
    "set_cached",
    "bump_stat",
    "flush_stats",
    "clean_title_for_compare",
    "format_article_display_title",
    "title_similarity",
    "find_similar_article",
    "rfc822_date",
    "escape_xml",
    "highlight_title",
    "extract_score_from_title",
    "extract_tag_from_title",
    "fetch_feed",
]
