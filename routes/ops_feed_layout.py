"""Component-width layout for Feed health, independent of installed Glance CSS."""

OPS_FEED_LAYOUT_STYLE = """<style id="ops-feed-component-layout">
.ops-feeds { container: ops-feeds / inline-size; min-width: 0; }
@container ops-feeds (max-width: 560px) {
  .ops-feeds > div:first-child { flex-wrap: wrap; }
  .ops-feeds .ops-feed-row {
    grid-template-columns: repeat(3, minmax(0, 1fr)) !important;
    min-width: 0;
    gap: 12px 8px !important;
  }
  .ops-feeds .ops-feed-row > div { min-width: 0; overflow-wrap: anywhere; }
  .ops-feeds .ops-feed-row > div:first-child { grid-column: 1 / 3; }
  .ops-feeds .ops-feed-row > div:first-child > div {
    white-space: normal !important;
    overflow: visible !important;
  }
  .ops-feeds .ops-feed-row > div:nth-child(3) { text-align: left !important; }
  .ops-feeds .ops-feed-row > div:nth-child(4) { text-align: center !important; }
}
</style>"""
