# farside_btc

Command-line fetcher for U.S. spot **Bitcoin ETF net flows**, scraped from
[Farside Investors](https://farside.co.uk/btc/). Emits a terminal briefing by
default, or structured JSON for piping into other tools.

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
fallback. The script also never returns partial current-day data — flows aren't
final until after the close, so the current day is excluded until Farside
publishes it (see [How it works](#how-it-works)).

The cache path (`~/.openclaw/cache/`) reflects that original consumer; it's just
a JSON file and isn't openclaw-specific.

---

## Features

- **Single-file, zero-config.** One Python script with inline [PEP 723](https://peps.python.org/pep-0723/)
  dependencies — run it directly with `uv` and nothing to install.
- **Bot-mitigation aware.** Uses `curl_cffi` with `impersonate="chrome"` to pass
  TLS fingerprint checks; falls back to a warmed `requests` session.
- **Schema-tolerant parser.** Locates the flow table by detecting date rows and
  maps columns by header name (`IBIT`, `FBTC`, `ARKB`, `GBTC`, `Total`) rather
  than fixed positions, so column reordering won't silently break it.
- **Derived metrics.** Rolling N-day net flow, inflow/outflow streak length, and
  an IBIT-share "conviction vs. breadth" classifier.
- **Caching + stale-fallback.** Caches the last good payload to
  `~/.openclaw/cache/farside_btc.json`; on fetch failure it returns the cached
  payload flagged `stale` instead of crashing.

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
chmod +x farside_btc.py   # once, if the exec bit was lost
./farside_btc.py
```

…or invoke uv explicitly (no exec bit needed):

```bash
uv run farside_btc.py
```

### Option B — pip

```bash
pip install -r requirements.txt
python3 farside_btc.py
```

Requires Python ≥ 3.11.

### Run it as a command (optional)

Symlink the script onto your PATH so you can invoke it as `farside_btc` from
anywhere (this is also the path the systemd unit's `ExecStart` resolves). Run
from the repo root:

```bash
chmod +x farside_btc.py
mkdir -p ~/.local/bin
ln -sf "$PWD/farside_btc.py" ~/.local/bin/farside_btc
farside_btc
```

The symlink is extensionless on purpose — the `uv run --script` shebang treats
the target as a script regardless of name. Ensure `~/.local/bin` is on your
`PATH` (it is by default on most modern distros).

---

## Usage

```bash
# Human-readable briefing block
./farside_btc.py

# Full structured detail
./farside_btc.py --json
```

### Example — default output

```
BTC ETF flows (Farside, as of 26 Jun 2026):
  latest: -444.5m total | -444.5m IBIT
  5d net: -1719.0m total | -1131.5m IBIT
  streak: 3d outflow
```

### Example — `--json`

```json
{
  "fetched_at": "2026-06-29T00:00:00+00:00",
  "stale": false,
  "summary": {
    "as_of": "26 Jun 2026",
    "age_days": 3,
    "pending_today": false,
    "latest_total": -444.5,
    "latest_ibit": -444.5,
    "window": 5,
    "window_total": -1719.0,
    "window_ibit": -1131.5,
    "streak_days": 3,
    "streak_sign": "outflow"
  },
  "rows": [ /* last `window` reported days, per-ETF */ ],
  "line": "ETF Flows: -444.5M (26 Jun) | 5d -1.72B | IBIT -1.13B (66%) | 3d outflow — conviction distribution"
}
```

All flow values are in **US$ millions**. Negative = net outflow.

---

## Output schema

`--json` returns a single object:

| Field         | Type            | Notes                                                        |
| ------------- | --------------- | ------------------------------------------------------------ |
| `fetched_at`  | ISO-8601 string | UTC time of the fetch                                        |
| `stale`       | bool            | `true` if served from cache after a fetch failure           |
| `error`       | string          | Present only when `stale` — the underlying exception         |
| `summary`     | object          | Derived metrics (see below)                                  |
| `rows`        | array           | Last `window` reported days, each with per-ETF flows         |
| `line`        | string          | One-line briefing with conviction/breadth tag                |

`summary` fields:

| Field           | Notes                                                              |
| --------------- | ----------------------------------------------------------------- |
| `as_of`         | Date of the latest *reported* day                                 |
| `age_days`      | Days since `as_of` (UTC)                                           |
| `pending_today` | `true` if the newest row exists but has no flows reported yet     |
| `latest_total`  | Most recent day's total net flow (US$m)                           |
| `latest_ibit`   | Most recent day's IBIT net flow (US$m)                            |
| `window`        | Rolling window length (default 5)                                 |
| `window_total`  | Net total flow over the window                                    |
| `window_ibit`   | Net IBIT flow over the window                                     |
| `streak_days`   | Consecutive same-sign total-flow days                             |
| `streak_sign`   | `inflow` \| `outflow` \| `flat`                                   |

The `line` classifier tags window flow as *conviction* vs *broad* based on
whether IBIT accounts for ≥60% of the same-signed window total.

---

## How it works

1. **Fetch** (`fetch_html`) — `curl_cffi` Chrome impersonation; falls back to a
   `requests.Session` that first warms `farside.co.uk/` to pick up cookies.
2. **Parse** (`parse_table`) — finds the first table whose rows start with a
   `D MMM YYYY` date, builds a header→column-index map, and reads per-ETF flows.
3. **Summarize** (`summarize`) — computes window nets, streak, and age over the
   *reported* rows. `_reported()` discards the current-day row until Farside
   publishes its numbers (surfaced as `pending_today`), so `as_of` is always the
   latest **finalized** day and running mid-session never yields partial data.
4. **Cache** (`save_cache` / `load_cache`) — writes the payload; on any fetch or
   parse error, `get_flows` returns the last cached payload flagged `stale`.

---

## Caching

Last good payload is written to:

```
~/.openclaw/cache/farside_btc.json
```

Delete it to force a clean state. The cache is what backs stale-fallback when
Farside is unreachable or changes its layout.

---

## Scheduling (systemd `--user`)

The intended deployment runs the script on a `--user` systemd timer that
refreshes the cache each evening (after U.S. ETF flows finalize), so a morning
consumer reads complete prior-day data. Ready-to-use units are in
[`deploy/systemd/`](deploy/systemd/).

`btc-flows.service`:

```ini
[Unit]
Description=Refresh Farside BTC ETF flow cache

[Service]
Type=oneshot
Environment=PATH=%h/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=%h/.local/bin/farside_btc
```

`btc-flows.timer`:

```ini
[Unit]
Description=Schedule Farside flow refresh

[Timer]
OnCalendar=*-*-* 22,23,01:30:00 UTC
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
- **Script location.** `ExecStart` runs `~/.local/bin/farside_btc`, a symlink to
  the repo file (see Install). The symlink gives a stable path independent of
  where the repo lives, and — since `~/.local/bin` is on PATH — also lets you run
  `farside_btc` as a bare command. The extension is dropped intentionally; the
  `uv run --script` shebang treats the target as a script regardless of name.

Install:

```bash
# from the repo root: make the script executable and symlink it onto PATH
chmod +x farside_btc.py
mkdir -p ~/.local/bin ~/.config/systemd/user
ln -sf "$PWD/farside_btc.py" ~/.local/bin/farside_btc

# install and start the timer
cp deploy/systemd/btc-flows.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now btc-flows.timer

# verify
systemctl --user list-timers btc-flows.timer
journalctl --user -u btc-flows.service -n 20
```

> On a headless box, enable lingering so `--user` timers run without an active
> login session: `loginctl enable-linger "$USER"`.

### cron equivalent

```cron
CRON_TZ=UTC
30 23 * * *  $HOME/.local/bin/farside_btc >> $HOME/.openclaw/cache/refresh.log 2>&1
30 1,2 * * *   $HOME/.local/bin/farside_btc >> $HOME/.openclaw/cache/refresh.log 2>&1
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
