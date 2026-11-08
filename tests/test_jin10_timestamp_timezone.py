"""金十 naive 时间戳必须按北京时间解释，不能依赖进程 TZ。

Jin10（以及 RSSHub 的 Jin10 路由）输出的墙钟字符串不带偏移量，但语义是北京
时间。用 ``time.mktime``/naive ``.timestamp()`` 转换会按进程时区解释：在 CI、
本地开发或任何未设 ``TZ=Asia/Shanghai`` 的主机上，每条新闻的时间会整体偏移
宿主时区与 +08:00 的差值（通常 8 小时），跨日条目因此进错日期桶。
"""

import calendar
import time

import pytest

from services.jin10_feed import _parse_timestamp

# 2026-07-13 20:03:53 Beijing == 2026-07-13 12:03:53 UTC
EXPECTED = calendar.timegm((2026, 7, 13, 12, 3, 53, 0, 0, 0))
EXPECTED_MINUTE = calendar.timegm((2026, 7, 13, 12, 3, 0, 0, 0, 0))


@pytest.fixture
def utc_process(monkeypatch):
    """Force a non-Beijing process zone; production compose uses Asia/Shanghai."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    assert time.strftime("%Y-%m-%d %H:%M", time.localtime(EXPECTED)) == "2026-07-13 12:03"
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-13 20:03:53",
        "2026-07-13T20:03:53",
        "Mon, 13 Jul 2026 20:03:53",
        "2026/07/13 20:03:53",
    ],
)
def test_naive_input_is_read_as_beijing_wall_clock(utc_process, value):
    assert calendar.timegm(_parse_timestamp(value)) == EXPECTED


def test_naive_minute_precision_is_read_as_beijing_wall_clock(utc_process):
    assert calendar.timegm(_parse_timestamp("2026-07-13 20:03")) == EXPECTED_MINUTE


def test_aware_input_still_uses_its_own_offset(utc_process):
    assert calendar.timegm(_parse_timestamp("2026-07-13T20:03:53+08:00")) == EXPECTED
    assert calendar.timegm(_parse_timestamp("2026-07-13T12:03:53Z")) == EXPECTED
