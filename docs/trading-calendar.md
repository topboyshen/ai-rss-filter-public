# A-share trading calendar

The trading calendar is data, not code. Both the application and the host ops
check read the same reviewed JSON file:

```
config/trading_calendar.json
```

Each year lists weekday market closures. Weekends are always closed and do not
need an entry. The validator accepts weekend dates inside a long holiday block
because the official notice includes them; they are harmless no-ops.

## Current coverage

| Year | Status |
|---|---|
| 2026 | Configured |
| 2027 | **Not published by the exchange as of 2026-11-28** |

The ops preflight looks 30 days ahead. It starts failing on **2026-12-02**
because the horizon reaches 2027. From **2026-12-04**, a T+20 target for a new
article can fall in 2027 and become unavailable.

## Update procedure

1. Read the official notice:
   <https://www.sse.com.cn/disclosure/dealinstruc/closed/list/>
2. Add the year to `config/trading_calendar.json`:

   ```json
   {
     "schema": 1,
     "years": {
       "2026": ["2026-01-01"],
       "2027": ["2027-01-01"]
     }
   }
   ```

   Keep every date in `YYYY-MM-DD` form. Do not remove an existing year.
3. Validate the file and the deadline:

   ```bash
   python -I scripts/check_trading_calendar.py --require-year 2027
   ```

   The command exits 0 only when the 30-day horizon and every required year
   are covered.
4. Restart the application services so `db/database.py` reloads the file.
5. Install the host ops check and the calendar file to the host paths it reads:

   ```bash
   bash ops/install.sh --apply
   ```

   This copies `config/trading_calendar.json` to
   `/etc/ai-filter/trading_calendar.json` (mode 0600) and the updated ops
   check to `/usr/local/sbin/ai-filter-ops-check`.
6. Confirm the preflight:

   ```bash
   /usr/local/sbin/ai-filter-ops-check --dry-run --json
   ```

   `trading_calendar` must be `ok=true`.

## Failure behaviour

An unknown year fails closed. `_is_trading_day_local` raises instead of
guessing weekdays, so backtests and close collection report unavailable data
rather than shifting the horizon. A malformed JSON file is ignored with a log
warning and the embedded fallback stays active.

The embedded 2026 table remains in both Python modules for rolling upgrades.
A test checks that it matches the JSON file.
