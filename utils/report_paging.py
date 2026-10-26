"""Pure, shared validation for bounded daily/weekly report reading links."""

import re

REPORT_BLOCK_KEYS = frozenset({"new", "advanced", "correction", "pending"})
# At most 140,000 report evidence rows, with 20 cards per page. This is a
# parameter safety bound, not a promise of that many complete report pages.
MAX_REPORT_PAGE = 7_000


def parse_weekly_paging(block=None, page=None) -> tuple[str | None, int]:
    """Require both selectors or neither; reject noncanonical numeric input."""
    if block is None and page is None:
        return None, 1
    if not isinstance(block, str) or block not in REPORT_BLOCK_KEYS:
        raise ValueError("invalid weekly report block")
    if type(page) is int:
        number = page
    elif isinstance(page, str) and re.fullmatch(r"[1-9][0-9]{0,3}", page):
        number = int(page)
    else:
        raise ValueError("invalid weekly report page")
    if not 1 <= number <= MAX_REPORT_PAGE:
        raise ValueError("weekly report page is out of range")
    return block, number


parse_report_paging = parse_weekly_paging
