# Publisher-source recovery (2026-09-29)

## Incident and scope

The `checks=metrics` notification at epoch `1790629201` was not a scheduler
outage. Natural collection continued; readiness reported `refresh_stale` and
`refresh_degraded`, most recently 18/20 successful sources. The four-hour
full-refresh threshold and partial-source failure rules remain unchanged.

The pinned RSSHub CSRC route expected JSON `data.results`. Its official
`searchList` endpoint returned a 302 to the homepage instead, producing a 503.
The old `common_list.shtml` still serves 2021 news and is **not** a valid repair.
The Caixin route fetched every article detail concurrently before applying the
feed limit; article connection timeouts made requests take 24–105 seconds,
exceeding this application's per-attempt 20-second timeout.

## Changes

- Only enabled, exactly configured internal `/gov/csrc/news` and
  `/caixin/latest` routes use native publisher adapters. Feed identities, entry
  limits, thresholds and notification choices are retained.
- CSRC: the current official news section at `/csrc/xwfb/index.shtml`, identified
  by its own `加载更多` link, provides the configured article URLs. At most three
  requests run concurrently; only configured articles are fetched. Original
  `ArticleTitle`, `PubDate` (UTC+8) and article body are required. No inferred
  year, synthetic date, archived list or stale-cache fallback.
- Caixin: the public scroll API provides original title, URL, publication time
  and **publisher public summary, not full text**. Preserve the RSSHub route's
  multimedia exclusions. One list request replaces unbounded detail fan-out;
  no paid-content credentials or model-generated evidence is used.
- The shared fetcher still enforces exact URL policy, pinned public DNS, TLS,
  redirect validation, response-size limits and bounded retries. Unexpected
  schema or missing required content fails the source; no successful-empty
  response is fabricated to clear a warning.
- Notification messages append only known red metric reason tokens, never raw
  failure details, URLs or credentials. Alert calculation, fingerprint and
  cooldown are unchanged.

## Contract and rollout

The publisher adapter is included in the strategy code fingerprint because
source input construction affects scoring. The processor dispatch also changes
the fingerprint. No scoring rules, model, mapping rules or history are changed;
new natural records use the newly measured contract. Do not blend contracts.

Use the normal clean SHA-bound gate/build/deploy/verify flow, preserving the
pre-deploy current as rollback. The host monitor must also be installed from
that same release: check its current hash first, atomically replace only
`/usr/local/sbin/ai-filter-ops-check` with mode 0700, and verify its hash. Do not
run a broad host installer, change schedules, erase alert state or send a test
recovery notification just to make the incident look resolved.

Fetch-only validation does not prove natural refresh recovery. Final acceptance
requires a later natural full refresh with 20/20 success, `stale=false` and
`degraded=false`, and the regular monitor's resulting state. If another source
fails, preserve and report that evidence; do not remove the source or widen the
threshold. External availability cannot be guaranteed permanently.
