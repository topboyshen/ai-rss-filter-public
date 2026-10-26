/* Offline browser regression; no production requests, credentials or database.
 * NODE_PATH=<playwright modules> node tests/browser/check_ops_feed_layout.cjs <test-python>
 * Set AI_FILTER_CHROME_PATH if using a locally installed Chromium executable.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '../..');
const python = process.argv[2] || 'python3';
const fixture = execFileSync(python, ['-c', `
import json, os, tempfile
with tempfile.TemporaryDirectory(prefix="ops-feed-layout-") as directory:
    os.environ["AI_FILTER_DB_PATH"] = directory + "/unused.db"
    from tests.test_ops_feed_component_layout import render_ops_feed_fixture
    from routes.ops_feed_layout import OPS_FEED_LAYOUT_STYLE
    print(json.dumps({"html": render_ops_feed_fixture(), "style": OPS_FEED_LAYOUT_STYLE}))
`], { cwd: root, encoding: 'utf8' });
const { html, style } = JSON.parse(fixture.trim().split('\n').at(-1));
const css = fs.readFileSync(path.join(root, 'scripts/glance_institutional_site.css'), 'utf8');
const documentFor = (feed, forcedWidth) => `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><style>
* { box-sizing: border-box; } body { margin: 0; } .content-bounds { margin-inline: auto; }
${css}
</style></head><body><main class="content-bounds"><div class="page"><div class="page-columns">
<div class="page-column"><div class="ai-site-shell ai-site-page-ops">运行监测</div></div>
<div class="page-column page-column-small"><div class="widget"><div class="widget-content">运行摘要</div></div></div>
<div class="page-column"><div class="widget"><div class="widget-content" ${forcedWidth ? `style="width:${forcedWidth}px"` : ''}>
${feed}</div></div></div></div></div></main></body></html>`;

async function measure(page, feed, width, forcedWidth) {
  await page.setViewportSize({ width, height: 1200 });
  await page.setContent(documentFor(feed, forcedWidth));
  return page.evaluate(() => {
    const component = document.querySelector('.ops-feeds');
    const row = document.querySelector('.ops-feed-row');
    const box = row.getBoundingClientRect();
    const children = [...row.children].map(element => {
      const rect = element.getBoundingClientRect();
      return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom,
        visible: getComputedStyle(element).display !== 'none', text: element.textContent };
    });
    return { viewport: innerWidth, document: document.documentElement.scrollWidth,
      container: component.clientWidth,
      headerRemainsFirstChild: component.firstElementChild.textContent.includes('Feed 源健康明细'),
      headerWrap: getComputedStyle(component.firstElementChild).flexWrap,
      row: { left: box.left, right: box.right, width: box.width },
      columns: getComputedStyle(row).gridTemplateColumns.split(' ').length, children,
      statAlignment: [2, 3].map(index => getComputedStyle(row.children[index]).textAlign),
      nameOverflow: getComputedStyle(row.firstElementChild.firstElementChild).overflow };
  });
}

async function run() {
  const executablePath = process.env.AI_FILTER_CHROME_PATH || (process.platform === 'darwin'
    ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : undefined);
  const browser = await chromium.launch({ headless: true, executablePath });
  try {
    const page = await browser.newPage();
    await page.route('**/*', route => route.abort());
    const legacy = await measure(page, html.replace(style, ''), 901);
    assert(legacy.document > 901, 'control must reproduce old 901px overflow');
    const results = [];
    for (const width of [320, 390, 750, 768, 900, 901, 950, 1000, 1024, 1440]) {
      const result = await measure(page, html, width);
      assert(result.document <= width + 1, `${width}px document overflows: ${result.document}`);
      assert(result.headerRemainsFirstChild, 'prepended style must not shift component nth-child selectors');
      assert(result.children.every(child => child.visible && child.text.trim()), 'content must remain visible');
      assert(result.children.every(child => child.left >= result.row.left - 1
        && child.right <= result.row.right + 1), `${width}px Feed child exceeds its row`);
      if ([320, 390, 901, 950].includes(width)) {
        assert.equal(result.columns, 3, `${width}px narrow component must reflow`);
        assert.equal(result.headerWrap, 'wrap', 'narrow Feed heading remains wrappable');
        assert.deepEqual(result.statAlignment, ['left', 'center'], 'stat cell order remains unchanged');
        assert.equal(result.nameOverflow, 'visible', 'long source names must not be clipped');
      }
      if (width === 1440) assert.equal(result.columns, 5, 'wide component keeps five columns');
      results.push(result);
    }
    // A desktop viewport can contain a narrow widget: breakpoints must follow it.
    const nested = await measure(page, html, 1440, 420);
    assert.equal(nested.columns, 3, 'reflow must depend on component, not viewport');
    assert(nested.children.every(child => child.right <= nested.row.right + 1));
    console.log(JSON.stringify({ legacy, results, narrowDesktopWidget: nested }));
  } finally {
    await browser.close();
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
