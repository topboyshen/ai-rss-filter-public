/* Offline regression for fresh/stale home grids. No live URLs or model calls.
 * NODE_PATH=<playwright modules> node tests/browser/check_home_snapshot_layout.cjs <test-python>
 */
const assert = require('node:assert/strict');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '../..');
const fixture = execFileSync(process.argv[2] || 'python3', ['-c', `
import json, os, sys, tempfile
from unittest.mock import patch
sys.path.insert(0, "tests")
with tempfile.TemporaryDirectory(prefix="home-snapshot-layout-") as directory:
    os.environ["AI_FILTER_DB_PATH"] = directory + "/unused.db"
    from test_snapshot_time_layout import _reading_shell
    from test_home_news_reading import _entry
    from test_glance_institutional_site import _installed_home
    from scripts.glance_institutional_site import site_layout
    from routes.dashboard_routes import (
        _prepend_realtime_snapshot_time, _prepend_stale_banner, _prepend_realtime_ingestion_coverage,
    )
    from services.ingestion_status import describe_refresh_coverage
    from test_home_ingestion_coverage import _attempt
    config = site_layout(_installed_home())
    cases = []
    for count in range(4):
        for grouped in (False, True):
            entries = [_entry(
                "创新药公司公布新药临床进展（样例%d）" % (index + 1),
                link="https://example.test/article/%d" % index,
                _realtime_section="fresh_strong" if grouped else "",
            ) for index in range(count)]
            fresh = _prepend_realtime_snapshot_time(_reading_shell(entries), "2026-10-02T10:00:00Z")
            covered = {}
            for status, coverage in (
                ("complete", describe_refresh_coverage(_attempt("success"))),
                ("incomplete", describe_refresh_coverage(_attempt(source="华尔街见闻全球" + "超长发布方名称" * 15))),
                ("unknown", describe_refresh_coverage(None)),
            ):
                with patch("services.ingestion_status.read_refresh_coverage", return_value=coverage):
                    covered["coverage_" + status] = _prepend_realtime_ingestion_coverage(fresh)[0]
            cases.append({"count": count, "grouped": grouped, "states": {
                "fresh": fresh,
                "expired": _prepend_stale_banner(fresh, "2026-09-01T10:00:00Z", 86400),
                "future": _prepend_stale_banner(fresh, "2030-01-01T10:00:00Z", -86400, reason="future"),
                **covered,
                "expired_incomplete": _prepend_stale_banner(covered["coverage_incomplete"], "2026-09-01T10:00:00Z", 86400),
            }})
    print(json.dumps({"head": config["document"]["head"],
        "masthead": config["pages"][0]["columns"][0]["widgets"][0]["source"], "cases": cases}))
`], { cwd: root, encoding: 'utf8', maxBuffer: 4 * 1024 * 1024 });
const data = JSON.parse(fixture.trim().split('\n').at(-1));
const documentFor = html => `<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><style>
* { box-sizing: border-box; } body { margin: 0; }
.content-bounds { margin-inline: auto; } h1,h2,h3,p { margin: 0; }
</style>${data.head}</head><body><main class="content-bounds"><div class="page">
<div class="page-columns"><div class="page-column page-column-full">
${data.masthead}<div class="widget ai-home-news"><div class="widget-header"><h2>近期新闻</h2></div>
<div class="widget-content">${html}</div></div></div>
<aside class="page-column page-column-small"><div class="widget ai-home-briefing">
<div class="widget-header"><h2>今日简报</h2></div><div class="widget-content" style="min-height:500px">独立简报内容</div>
</div></aside></div></div></main></body></html>`;

async function measure(page, html, width, testDisclosure) {
  await page.setViewportSize({ width, height: 1200 });
  await page.setContent(documentFor(html));
  const result = await page.evaluate(() => {
    const rect = node => {
      const box = node.getBoundingClientRect();
      return { top: box.top, bottom: box.bottom, left: box.left, right: box.right };
    };
    const signature = node => `${node.tagName}.${node.className}#${node.id}`;
    const gridChildren = node => [...node.children].flatMap(child => {
      const display = getComputedStyle(child).display;
      return display === 'none' ? [] : display === 'contents' ? gridChildren(child) : [signature(child)];
    });
    const articles = [...document.querySelectorAll('.home-news-card')];
    const warning = document.querySelector('.dashboard-snapshot-warning');
    const coverage = document.querySelector('.dashboard-ingestion-coverage');
    return {
      width: innerWidth, document: document.documentElement.scrollWidth,
      gridSignature: gridChildren(document.querySelector('.page-columns')),
      cards: articles.map(article => ({ ...rect(article),
        link: article.querySelector('h3 a').getAttribute('href'),
        title: article.querySelector('h3').textContent,
        adjacentPrevious: article.previousElementSibling?.tagName === 'ARTICLE',
        details: [...article.querySelectorAll('details')].map(detail => ({
          summary: detail.querySelector('summary').textContent, open: detail.open,
        })),
      })),
      sidebar: rect(document.querySelector('aside')),
      warning: warning ? { ...rect(warning), text: warning.textContent,
        nestedInWindow: Boolean(warning.closest('.home-news-window')),
        visible: getComputedStyle(warning).display !== 'none' && warning.getBoundingClientRect().height > 0 } : null,
      coverage: coverage ? { ...rect(coverage), text: coverage.textContent,
        nestedInWindow: Boolean(coverage.closest('.home-news-window')),
        visible: getComputedStyle(coverage).display !== 'none' && coverage.getBoundingClientRect().height > 0 } : null,
    };
  });
  if (testDisclosure) {
    const summary = page.locator('.home-news-card details > summary').first();
    await summary.click();
    assert(await page.locator('.home-news-card details').first().evaluate(node => node.open));
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await summary.click();
    assert.equal(await page.locator('.home-news-card details').first().evaluate(node => node.open), false);
    result.disclosureTogglePassed = true;
  }
  return result;
}

function verify(result, count, state, fresh) {
  assert(result.document <= result.width + 1, `${count}/${state}/${result.width}: horizontal overflow`);
  assert.equal(result.cards.length, count);
  assert(result.cards.every(card => card.details.length && card.details.every(detail => !detail.open)));
  if (count > 1 && result.width > 900 && result.cards[1].adjacentPrevious) {
    assert(Math.abs(result.cards[0].top - result.cards[1].top) < 1, 'desktop split feature must stay aligned');
  }
  if (count > 2 && result.width > 900) {
    assert(result.cards[2].top < result.sidebar.bottom - 100, 'third card must not wait until tall sidebar ends');
  }
  if (!state.startsWith('expired') && state !== 'future') assert.equal(result.warning, null);
  else {
    assert(result.warning?.visible && result.warning.nestedInWindow, 'warning remains visible in existing header');
    assert(result.warning.text.includes(state === 'future' ? '快照时间异常' : '数据已过期'));
  }
  if (state.startsWith('coverage_') || state === 'expired_incomplete') {
    assert(result.coverage?.visible && result.coverage.nestedInWindow, 'collection coverage remains in existing header');
    assert(result.coverage.right <= result.width + 1 && result.coverage.left >= -1, 'long source names must wrap');
    assert(result.coverage.text.includes(state.endsWith('unknown') ? '状态未知'
      : state.endsWith('incomplete') ? '19/20' : '20/20'));
  }
  if (state !== 'fresh') {
    assert.deepEqual(result.gridSignature, fresh.gridSignature, 'notices must not add a root grid item');
    assert.deepEqual(result.cards.map(({ link, title, details }) => ({ link, title, details })),
      fresh.cards.map(({ link, title, details }) => ({ link, title, details })), 'articles/order/disclosures must remain unchanged');
    if (count) {
      const shift = result.cards[0].top - fresh.cards[0].top;
      result.cards.forEach((card, index) => assert(Math.abs(card.top - fresh.cards[index].top - shift) < 1,
        'warning may increase header height only, not separate later cards'));
    }
  }
}

async function run() {
  const executablePath = process.env.AI_FILTER_CHROME_PATH || (process.platform === 'darwin'
    ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : undefined);
  const browser = await chromium.launch({ headless: true, executablePath });
  try {
    const page = await browser.newPage();
    await page.route('**/*', route => route.abort());
    const control = data.cases.find(item => item.count === 3 && item.grouped);
    const banner = control.states.expired.match(/<div class="dashboard-snapshot-warning"[\s\S]*?<\/div>/)[0];
    const legacy = await measure(page, banner + control.states.fresh, 1440, false);
    assert(Math.abs(legacy.cards[0].top - legacy.cards[1].top) > 100,
      'old root-banner control must reproduce separated feature cards');
    const results = [];
    for (const fixture of data.cases) {
      for (const width of [1440, 1024, 901, 390, 320]) {
        let fresh;
        for (const [state, html] of Object.entries(fixture.states)) {
          const result = await measure(page, html, width, fixture.count === 3 && fixture.grouped);
          if (state === 'fresh') fresh = result;
          verify(result, fixture.count, state, fresh);
          results.push({ count: fixture.count, grouped: fixture.grouped, state, ...result });
        }
      }
    }
    console.log(JSON.stringify({ styleOrder: [...data.head.matchAll(/<style id="([^"]+)"/g)].map(match => match[1]),
      legacyRootBanner: legacy, results }));
  } finally {
    await browser.close();
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
