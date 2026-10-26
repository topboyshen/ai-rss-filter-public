"""Presentation-only wrapper around the existing quote-cache HTML."""


def fold_market_quotes(html: str) -> str:
    return (
        '<details class="reading-market" style="font-size:13px;line-height:1.6;">'
        '<summary style="cursor:pointer;padding:10px 0;">指数行情 · 展开查看背景</summary>'
        '<p style="margin:0 0 12px;color:var(--color-text-subdue,inherit);">'
        '市场背景，不是新闻评分依据；以下为既有行情缓存，不代表本页打开时的报价。</p>'
        + html + '</details>'
    )
