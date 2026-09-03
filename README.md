# farside_flows

Command-line fetcher for U.S. spot **crypto ETF net flows** (Bitcoin, Ethereum,
and Solana), scraped from [Farside Investors](https://farside.co.uk/). Emits a
terminal briefing by default, or structured JSON for piping into other tools.
BTC is the default; select an asset with a positional argument.

It handles the parts that make scraping Farside annoying: TLS/JA3 fingerprinting
(via Chrome impersonation), schema-tolerant table parsing, on-disk caching, and
graceful stale-fallback when the upstream fetch fails.

---

## Intended use

This was built as the data source for an openclaw-managed **morning briefing
agent**. The deployment model:

- A **systemd `--user` timer refreshes the cache each evening**, after U.S. ETF
  flow numbers finalize, writing the complete prior trading day to
  `~/.openclaw/cache/farside_btc.json`.
- A **consumer invokes the script**, which fetches live and — crucially — falls
  back to that cached prior-evening payload (marked `stale`) only if the live
  fetch fails. The briefing always has recent, complete data, online or not.

The cache is a **resilience layer, not a no-fetch read path**: a direct
invocation always tries the live URL first and uses the cache solely as a
fallback. The script also never folds partial data into its metrics — a day is
treated as final only once **every tracked fund** has reported. Farside posts
funds progressively through the day, so any day with an outstanding fund is
excluded from all latest/streak/window figures and surfaced separately as
`partial` (see [How it works](#how-it-works)).

The cache path (`~/.openclaw/cache/`) reflects that original consumer; it's just
a JSON file and isn't openclaw-specific.

---

## Features

- **Single-file, zero-config.** One Python script with inline [PEP 723](https://peps.python.org/pep-0723/)
  dependencies — run it directly with `uv` and nothing to install.
- **Bot-mitigation aware.** Uses `curl_cffi` with `impersonate="chrome"` to pass
  TLS fingerprint checks; falls back to a warmed `requests` session.
- **Multiple assets.** Fetches the Bitcoin, Ethereum, or Solana flow table via a
  positional argument (`btc` default); each asset has its own flagship ("lead")
  fund and cache file.
- **Schema-tolerant parser.** Locates the flow table by detecting date rows and
  maps columns by header name (per-asset tickers + `Total`) rather than fixed
  positions, so column reordering won't silently break it.
- **Reported-zero vs. not-reported.** Distinguishes a fund's genuine `0.0` flow
  from a not-yet-published cell (Farside renders the latter as `-`), so a
  pending fund is never silently counted as a zero. A day counts as final only
  once every tracked fund has reported; partial days are excluded from metrics
  and exposed via `partial`/`partial_pending`.
- **Derived metrics.** Rolling 5/20/60-day net flows, inflow/outflow streak
  length, and a lead-fund-share classifier (*conviction* / *broad* /
  *offsetting*).
- **Full history, honestly bounded.** Pulls each asset's "all data" page (BTC
  back to Jan 2024, ETH to Jul 2024) so the long windows have real history, and
  falls back to the shorter nav page if it's unavailable. A window the source
  can't fill reports as `n/a` with the day count available, never a shorter net
  under a longer label.
- **Caching + stale-fallback.** Caches the last good payload per asset to
  `~/.openclaw/cache/farside_<asset>.json`; on fetch failure it returns the
  cached payload flagged `stale` instead of crashing.

---

## Install

### Option A — uv (recommended)

Requires [uv](https://docs.astral.sh/uv/) to be installed (one-time):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

The script declares its own Python dependencies inline ([PEP 723](https://peps.python.org/pep-0723/)),
so there's nothing else to install — uv resolves them into an ephemeral
environment on first run. The shebang (`#!/usr/bin/env -S uv run --script`)
routes execution through uv, so you can run the file directly:

```bash
chmod +x farside_flows.py   # once, if the exec bit was lost
./farside_flows.py
```

…or invoke uv explicitly (no exec bit needed):

```bash
uv run farside_flows.py
```

### Option B — pip

```bash
pip install -r requirements.txt
python3 farside_flows.py
```

Requires Python ≥ 3.11.

### Run it as a command (optional)

Symlink the script onto your PATH so you can invoke it as `farside_flows` from
anywhere (this is also the path the systemd unit's `ExecStart` resolves). Run
from the repo root:

```bash
chmod +x farside_flows.py
mkdir -p ~/.local/bin
ln -sf "$PWD/farside_flows.py" ~/.local/bin/farside_flows
farside_flows
```

The symlink is extensionless on purpose — the `uv run --script` shebang treats
the target as a script regardless of name. Ensure `~/.local/bin` is on your
`PATH` (it is by default on most modern distros).

---

## Usage

```bash
# Human-readable briefing block (BTC by default)
./farside_flows.py

# Pick an asset: btc (default), eth, or sol
./farside_flows.py eth
./farside_flows.py sol

# Full structured detail
./farside_flows.py --json
./farside_flows.py eth --json
```

### Example — default output

```
BTC ETF flows (Farside, as of 26 Jun 2026):
  latest: -444.5m total | -444.5m IBIT
  5d net: -1719.0m total | -1131.5m IBIT
  20d net: -49.6m total | -135.3m IBIT
  60d net: -6742.7m total | -4886.9m IBIT
  streak: 3d outflow
```

Solana has no full-history page upstream, so its long windows stay `n/a` until
enough trading days accumulate on the nav page:

```
SOL ETF flows (Farside, as of 26 Jun 2026):
  latest: -18.1m total | -18.1m BSOL
  5d net: -10.0m total | -8.7m BSOL
  20d net: n/a (9d available)
  60d net: n/a (9d available)
  streak: 1d outflow
```

### Example — `--json`

```json
{
  "fetched_at": "2026-06-29T00:00:00+00:00",
  "source": "https://farside.co.uk/bitcoin-etf-flow-all-data/",
  "stale": false,
  "summary": {
    "asset": "btc",
    "lead": "IBIT",
    "as_of": "26 Jun 2026",
    "age_days": 3,
    "pending_today": false,
    "partial_pending": false,
    "partial": null,
    "latest_total": -444.5,
    "latest_lead": -444.5,
    "days_complete": 638,
    "windows": [
      {
        "days": 5,
        "days_available": 5,
        "covered": true,
        "total": -1719.0,
        "lead": -1131.5,
        "dates": [ "20 Jun 2026", "23 Jun 2026", "24 Jun 2026", "25 Jun 2026", "26 Jun 2026" ]
      },
      { "days": 20, "days_available": 20, "covered": true, "total": -49.6, "lead": -135.3, "dates": [ /* 20 dates */ ] },
      { "days": 60, "days_available": 60, "covered": true, "total": -6742.7, "lead": -4886.9, "dates": [ /* 60 dates */ ] }
    ],
    "window": 5,
    "window_dates": [ "20 Jun 2026", "23 Jun 2026", "24 Jun 2026", "25 Jun 2026", "26 Jun 2026" ],
    "window_total": -1719.0,
    "window_lead": -1131.5,
    "streak_days": 3,
    "streak_sign": "outflow"
  },
  "rows": [ /* last 5 reported days; per-fund + `Other` + `Total` */ ],
  "line": "BTC ETF Flows: -444.5M (26 Jun, IBIT -444.5M) | 5d net -1.72B | IBIT 5d -1.13B (66%) | 20d -49.6M | 60d -6.74B | 3d outflow — conviction distribution"
}
```

For `eth`/`sol` the shape is identical; `asset`/`lead` and the `rows` tickers
change (e.g. `"asset": "eth"`, `"lead": "ETHA"`).

All flow values are in **US$ millions**. Negative = net outflow.

---

## Output schema

`--json` returns a single object:

| Field         | Type            | Notes                                                        |
| ------------- | --------------- | ------------------------------------------------------------ |
| `fetched_at`  | ISO-8601 string | UTC time of the fetch                                        |
| `source`      | string          | Page the table came from — the all-data page, or the nav page if it fell back |
| `stale`       | bool            | `true` if served from cache after a fetch failure           |
| `error`       | string          | Present only when `stale` — the underlying exception         |
| `summary`     | object          | Derived metrics (see below)                                  |
| `rows`        | array           | Last 5 reported days (`DEFAULT_ROWS`, independent of the windows); each has the tracked funds, `Other`, and `Total` (see below) |
| `line`        | string          | One-line briefing with conviction/breadth tag                |

`summary` fields:

| Field           | Notes                                                              |
| --------------- | ----------------------------------------------------------------- |
| `asset`         | `btc` \| `eth` \| `sol`                                           |
| `lead`          | Flagship fund ticker for the asset (`IBIT` / `ETHA` / `BSOL`)     |
| `as_of`         | Date of the latest *fully-reported* day (all tracked funds in)     |
| `age_days`      | Days since `as_of` (UTC)                                           |
| `pending_today` | `true` if the newest row exists but has no flows reported yet     |
| `partial_pending` | `true` if the newest reported day has some but not all tracked funds in |
| `partial`       | Object for that in-progress day, else `null` (see below)          |
| `latest_total`  | Most recent *fully-reported* day's total net flow (US$m)          |
| `latest_lead`   | Most recent *fully-reported* day's lead-fund net flow (US$m)      |
| `days_complete` | How many fully-reported days the source supplied — the ceiling on window coverage |
| `windows`       | Array of per-window nets, in requested order (see below)          |
| `window`        | Primary (first) window's length — legacy alias                    |
| `window_dates`  | Primary window's `dates` — legacy alias (may differ from `rows`, which lists the most recent *reported* days incl. any partial one) |
| `window_total`  | Primary window's `total` — legacy alias                           |
| `window_lead`   | Primary window's `lead` — legacy alias                            |
| `streak_days`   | Consecutive same-sign total-flow days                             |
| `streak_sign`   | `inflow` \| `outflow` \| `flat`                                   |

Each `windows` entry:

| Field             | Notes                                                          |
| ----------------- | -------------------------------------------------------------- |
| `days`            | Requested window length (5, 20, 60 by default)                 |
| `days_available`  | Fully-reported days actually available for it                  |
| `covered`         | `false` when `days_available < days` — the source has too little history |
| `total`           | Net total flow over the window; `null` when `covered` is `false` |
| `lead`            | Net lead-fund flow over the window; `null` when `covered` is `false` |
| `dates`           | The fully-reported days the nets cover (the partial span when uncovered) |

**Never read `total` without checking `covered`.** An uncovered window returns
`null` rather than a shorter window's net wearing a longer window's label —
consumers that ignore the flag and default `null` to `0` will misreport. Only
the **primary** (first) window drives the `line` classifier; if it is uncovered
the tag reads *insufficient history*. Windows are configurable via
`summarize(data, cfg, windows=...)` / `get_flows(asset, windows=...)`; a bare
int is still accepted for a single window.

When present, `partial` describes the in-progress latest day:

| Field            | Notes                                                            |
| ---------------- | --------------------------------------------------------------- |
| `date`           | The partial day's date                                          |
| `reported_total` | Net flow of the *tracked* funds that have reported so far (US$m) |
| `other`          | Net of the untracked funds in `Total` (see `Other` below); `reported_total + other == Total` |
| `reported`       | Tracked tickers that have posted                                |
| `pending`        | Tracked tickers still outstanding                              |

**Tracked funds vs. `Total`.** Farside's `Total` column sums *every* listed
ETF, but each asset itemizes only a curated subset (the flagship + a few majors).
Each `rows` entry therefore carries an **`Other`** field — `Total` minus the
tracked funds present — capturing the aggregate of the untracked funds, so that
`sum(tracked present) + Other == Total` for every row. Do **not** infer a pending
fund's value from `Total − reported_total`; that residual is `other` (untracked
flow), not the outstanding fund.

The `line` classifier tags the same-signed window flow by the lead fund's
share: *conviction* when the lead is 60–120% of the window total, *offsetting*
when it exceeds 120% (other funds net-offset it, leaving a small residual), and
*broad* below 60%.

---

## How it works

1. **Fetch** (`fetch_table` / `fetch_html`) — `fetch_table` tries the asset's
   full-history "all data" page first and falls back to the shorter nav page if
   it 404s or fails to parse (Solana has no all-data page, so it uses the nav
   page directly). `fetch_html` does the transport: `curl_cffi` Chrome
   impersonation, falling back to a `requests.Session` that first warms
   `farside.co.uk/` to pick up cookies.
2. **Parse** (`parse_table` / `parse_flow`) — finds the first table whose rows
   start with a `D MMM YYYY` date, builds a header→column-index map, and reads
   per-fund flows. A genuine `0.0` flow is kept as `0.0`; a not-yet-published
   cell (Farside renders it as `-` or blank) becomes `None`, so a pending fund
   is never coerced into a zero.
3. **Summarize** (`summarize`) — computes each window's nets, streak, and age over
   *fully-reported* days only (`complete` = every tracked fund non-`None`). A
   day with all funds still blank is dropped by `_reported()` and flagged
   `pending_today`; a day with some funds in but others outstanding is excluded
   from the metrics and flagged `partial_pending`, with its known state exposed
   in `partial`. So `as_of` is always the latest **finalized** day and running
   mid-session never folds partial data into the totals. A window with fewer
   than `days` complete days is marked `covered: false` with `null` nets rather
   than silently netting a shorter span.
4. **Cache** (`save_cache` / `load_cache`) — writes the payload; on any fetch or
   parse error, `get_flows` returns the last cached payload flagged `stale`.

---

## Caching

Each asset's last good payload is written to its own file:

```
~/.openclaw/cache/farside_<asset>.json   # e.g. farside_btc.json, farside_eth.json
```

Delete a file to force a clean state for that asset. The cache is what backs
stale-fallback when Farside is unreachable or changes its layout.

---

## Scheduling (systemd `--user`)

The intended deployment runs the script on a `--user` systemd timer that
refreshes the cache each evening (after U.S. ETF flows finalize), so a morning
consumer reads complete prior-day data. Ready-to-use units are in
[`deploy/systemd/`](deploy/systemd/).

`farside-flows.service`:

```ini
[Unit]
Description=Refresh Farside ETF flow cache

[Service]
Type=oneshot
Environment=PATH=%h/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=%h/.local/bin/farside_flows
```

`farside-flows.timer`:

```ini
[Unit]
Description=Schedule Farside flow refresh

[Timer]
OnCalendar=*-*-* 03,04,05:30:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

Notes:

- **Timing.** `OnCalendar` fires at 23:30, 1:30, and 02:30 UTC — three
  evening-to-night (U.S. Eastern) attempts to capture the finalized
  prior-day flows and survive a late publish or a transient fetch failure. Each
  run overwrites the cache.
- **`Persistent=true`** re-runs a missed timer after the machine boots/wakes, so
  a laptop that was asleep still refreshes on next start.
- **uv on PATH.** systemd `--user` services start with a minimal PATH. The
  `Environment=PATH=...` line prepends `~/.local/bin` so the script's `uv run`
  shebang resolves (uv's default install location). Adjust if uv lives elsewhere.
- **Script location.** `ExecStart` runs `~/.local/bin/farside_flows`, a symlink to
  the repo file (see Install). The symlink gives a stable path independent of
  where the repo lives, and — since `~/.local/bin` is on PATH — also lets you run
  `farside_flows` as a bare command. The extension is dropped intentionally; the
  `uv run --script` shebang treats the target as a script regardless of name.

Install:

```bash
# from the repo root: make the script executable and symlink it onto PATH
chmod +x farside_flows.py
mkdir -p ~/.local/bin ~/.config/systemd/user
ln -sf "$PWD/farside_flows.py" ~/.local/bin/farside_flows

# install and start the timer
cp deploy/systemd/farside-flows.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now farside-flows.timer

# verify
systemctl --user list-timers farside-flows.timer
journalctl --user -u farside-flows.service -n 20
```

> On a headless box, enable lingering so `--user` timers run without an active
> login session: `loginctl enable-linger "$USER"`.

### Refreshing additional assets

The bundled `farside-flows.timer` refreshes BTC only.

> **Check first whether something already refreshes the other assets.** Any
> external scheduler that invokes `farside_flows <asset>` covers this already —
> on the author's box the openclaw scheduler drives ETH and SOL, so the units
> below would refresh those assets twice. The timers here are for a standalone
> install.
>
> A stale `farside_<asset>.json` is not on its own evidence that no schedule
> exists: nothing in this repo, and nothing in `systemctl --user list-timers`,
> can see an external one.

To cover ETH and SOL from systemd instead, either duplicate the unit pair with
the asset in `ExecStart` (e.g. `ExecStart=%h/.local/bin/farside_flows eth`), or
— cleaner — replace the single unit with a systemd template so one pair serves
every asset (`%i` is the instance name):

```ini
# ~/.config/systemd/user/farside-flows@.service
[Unit]
Description=Refresh Farside %i ETF flow cache

[Service]
Type=oneshot
Environment=PATH=%h/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=%h/.local/bin/farside_flows %i
```

```ini
# ~/.config/systemd/user/farside-flows@.timer
[Unit]
Description=Schedule Farside %i flow refresh

[Timer]
OnCalendar=*-*-* 23,01,02:30:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now farside-flows@btc.timer farside-flows@eth.timer farside-flows@sol.timer
```

If you enable the template's `@btc` instance, disable the bundled
`farside-flows.timer` so BTC isn't refreshed twice.

### cron equivalent

```cron
CRON_TZ=UTC
30 23 * * *  $HOME/.local/bin/farside_flows >> $HOME/.openclaw/cache/refresh.log 2>&1
30 1,2 * * *   $HOME/.local/bin/farside_flows >> $HOME/.openclaw/cache/refresh.log 2>&1
```

---

## Disclaimer

Data is scraped from Farside Investors and provided **as is**, with no guarantee
of accuracy, completeness, or timeliness. Farside generates its table
automatically and disclaims liability for errors. This tool is for
informational purposes only and is **not** investment advice. Respect Farside's
terms of use and don't hammer their site.

---

## License

[MIT](LICENSE) © 2026 Michael OConnor
