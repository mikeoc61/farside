#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["curl_cffi", "beautifulsoup4", "requests"]
# ///
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

URL = "https://farside.co.uk/btc/"
CACHE = Path.home() / ".openclaw" / "cache" / "farside_btc.json"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
DATE_RE = re.compile(r"^\d{1,2}\s+[A-Za-z]{3}\s+\d{4}$")
WANT = ("IBIT", "FBTC", "ARKB", "GBTC", "Total")


def parse_flow(s):
    s = s.strip().replace(",", "").replace("\u2013", "-")
    if s in ("", "-"):
        return 0.0
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def fetch_html(url=URL, timeout=20):
    try:
        from curl_cffi import requests as creq
    except ImportError:
        creq = None
    if creq is not None:
        r = creq.get(url, headers=HEADERS, timeout=timeout, impersonate="chrome")
        r.raise_for_status()
        return r.text
    import requests
    sess = requests.Session()
    sess.headers.update(HEADERS)
    sess.get("https://farside.co.uk/", timeout=timeout)
    r = sess.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def parse_table(html):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = [
            [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
            for tr in table.find_all("tr")
        ]
        first_data = next(
            (i for i, c in enumerate(rows) if c and DATE_RE.match(c[0])), None
        )
        if first_data is None:
            continue
        colmap = {}
        for cells in rows[:first_data]:
            for idx, text in enumerate(cells):
                if text in WANT and text not in colmap:
                    colmap[text] = idx
        if "IBIT" not in colmap or "Total" not in colmap:
            continue
        data = []
        for cells in rows[first_data:]:
            if cells and DATE_RE.match(cells[0]):
                rec = {"date": cells[0]}
                for name in WANT:
                    idx = colmap.get(name)
                    rec[name] = (
                        parse_flow(cells[idx])
                        if idx is not None and idx < len(cells)
                        else None
                    )
                data.append(rec)
        if data:
            return data
    raise ValueError("flow table not found or schema changed")


def _age_days(date_str):
    try:
        d = datetime.strptime(date_str, "%d %b %Y").date()
        return (datetime.now(timezone.utc).date() - d).days
    except ValueError:
        return None


def _reported(data):
    return [
        r for r in data
        if any(r.get(k) not in (None, 0.0) for k in WANT)
    ]


def summarize(data, window=5):
    reported = _reported(data)
    pending = bool(data) and bool(reported) and data[-1] is not reported[-1]
    if not reported:
        return {
            "as_of": None,
            "age_days": None,
            "pending_today": pending,
            "latest_total": None,
            "latest_ibit": None,
            "window": window,
            "window_total": 0.0,
            "window_ibit": 0.0,
            "streak_days": 0,
            "streak_sign": "flat",
        }
    recent = reported[-window:]
    latest = reported[-1]
    total_w = sum(r["Total"] for r in recent if r["Total"] is not None)
    ibit_w = sum(r["IBIT"] for r in recent if r["IBIT"] is not None)
    sign = None
    streak = 0
    for r in reversed(reported):
        v = r["Total"]
        if v is None:
            break
        s = 1 if v > 0 else (-1 if v < 0 else 0)
        if sign is None:
            sign, streak = s, 1
        elif s == sign and s != 0:
            streak += 1
        else:
            break
    return {
        "as_of": latest["date"],
        "age_days": _age_days(latest["date"]),
        "pending_today": pending,
        "latest_total": latest["Total"],
        "latest_ibit": latest["IBIT"],
        "window": window,
        "window_total": round(total_w, 1),
        "window_ibit": round(ibit_w, 1),
        "streak_days": streak,
        "streak_sign": (
            "inflow" if sign and sign > 0
            else "outflow" if sign and sign < 0
            else "flat"
        ),
    }


def load_cache():
    try:
        return json.loads(CACHE.read_text())
    except Exception:
        return None


def save_cache(payload):
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(payload, indent=2))


def get_flows(window=5):
    try:
        data = parse_table(fetch_html())
        payload = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "stale": False,
            "summary": summarize(data, window),
            "rows": _reported(data)[-window:],
        }
        payload["line"] = briefing_line(payload)
        save_cache(payload)
        return payload
    except Exception as e:
        cached = load_cache()
        if cached is None:
            raise
        cached["stale"] = True
        cached["error"] = str(e)
        return cached


def _fmt(v):
    if v is None:
        return "n/a"
    return f"+{v:.1f}" if v >= 0 else f"{v:.1f}"


def briefing_block(payload):
    s = payload["summary"]
    flags = []
    if payload.get("stale"):
        flags.append("FETCH-FAILED")
    if s.get("pending_today"):
        flags.append("TODAY-PENDING")
    if s.get("age_days") is not None and s["age_days"] > 4:
        flags.append(f"DATA-{s['age_days']}D-OLD")
    tag = f" [{', '.join(flags)}]" if flags else ""
    return "\n".join([
        f"BTC ETF flows (Farside, as of {s['as_of']}){tag}:",
        f"  latest: {_fmt(s['latest_total'])}m total | {_fmt(s['latest_ibit'])}m IBIT",
        f"  {s['window']}d net: {_fmt(s['window_total'])}m total | {_fmt(s['window_ibit'])}m IBIT",
        f"  streak: {s['streak_days']}d {s['streak_sign']}",
    ])


def _abbr(v):
    if v is None:
        return "n/a"
    if abs(v) >= 1000:
        return f"{v / 1000:+.2f}B"
    return f"{v:+.1f}M"


def briefing_line(payload):
    s = payload["summary"]
    if s["as_of"] is None:
        return "ETF Flows: n/a"
    wt, wi = s["window_total"], s["window_ibit"]
    share_txt, share_val = "", None
    if wt and wi is not None and (wt > 0) == (wi > 0):
        share_val = round(100 * wi / wt)
        share_txt = f" ({share_val}%)"
    if wt < 0:
        tag = "conviction distribution" if share_val and share_val >= 60 else "broad outflow"
    elif wt > 0:
        tag = "conviction accumulation" if share_val and share_val >= 60 else "broad inflow"
    else:
        tag = "mixed flows"
    asof_short = " ".join(s["as_of"].split()[:2])
    line = (
        f"ETF Flows: {_abbr(s['latest_total'])} ({asof_short}) | "
        f"{s['window']}d {_abbr(wt)} | IBIT {_abbr(wi)}{share_txt} | "
        f"{s['streak_days']}d {s['streak_sign']} — {tag}"
    )
    notes = []
    if s.get("pending_today"):
        notes.append("today pending")
    if payload.get("stale"):
        notes.append("data stale")
    if notes:
        line += "; " + "; ".join(notes)
    return line


if __name__ == "__main__":
    out = get_flows()
    if "--json" in sys.argv:
        print(json.dumps(out, indent=2))
    else:
        print(briefing_block(out))
