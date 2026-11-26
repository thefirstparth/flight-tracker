#!/usr/bin/env python3
"""Build the price dashboard: one self-contained HTML file.

The page has every price check, chart and flight list inlined, makes no
network requests, and works offline on a phone or computer.

Usage:
  python dashboard.py                      build dashboard/index.html from data/
  python dashboard.py --demo               build a demo page from made-up history
  python dashboard.py --out some/file.html
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import random
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import tracker
from store import FileStore

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "dashboard_template.html"
DATA_MARKER = "/*__DASHBOARD_DATA__*/null"


def _compress(rows: list[dict]) -> list[list]:
    """Keep only the checks where a watch's price changed, plus the last one.

    The chart draws prices as steps, so nothing visible is lost. A missing
    flight is stored as null, which breaks the line.
    """
    points, last = [], object()
    for i, r in enumerate(rows):
        price = float(r["price"]) if r.get("price") else None
        if price != last or i == len(rows) - 1:
            points.append([r["checked_at"], price])
            last = price
    return points


def collect(config: Path, store) -> dict:
    """Everything the page shows, from a FileStore/RedisStore (or a data folder)."""
    if isinstance(store, (str, Path)):
        store = FileStore(Path(store))
    settings, watches = tracker.load_config(config)
    state = store.load_state()
    history = store.load_history([w.id for w in watches]) if store.kind == "redis" else store.load_history()
    events = store.load_events()
    latest = store.load_latest()

    by_watch: dict[str, list[dict]] = {}
    for row in history:
        by_watch.setdefault(row["watch_id"], []).append(row)

    searches = latest.get("searches", {})
    trips: dict[str, dict] = {}
    out_watches = []
    for w in watches:
        st = state.get("watches", {}).get(w.id, {})
        points = _compress(sorted(by_watch.get(w.id, []), key=lambda r: r["checked_at"]))
        # The flights behind today's price, and the other choices that fit the watch
        # (e.g. each direct return when the return is "any").
        opts = [tracker.Option(o["price"], tuple(o["flights"]), (), tuple(o.get("legs") or ()))
                for o in (searches.get(w.search_id) or {}).get("options", [])]
        fitting = sorted(tracker.matching_options(w, opts), key=lambda o: o.price)
        current = next((o for o in fitting if list(o.flights) == (st.get("last_flights") or [])), None)
        prices = [pr for _, pr in points if pr is not None]
        # "Was" price, shown struck through: the price before the last move of
        # at least ₹100 (smaller moves are mostly exchange-rate noise).
        prev = next((pr for pr in reversed(prices[:-1]) if abs(pr - prices[-1]) >= 100), None) if prices else None
        out_watches.append({
            "id": w.id, "name": w.name, "trip": w.trip, "who": w.who,
            "from": w.origin, "to": w.destination, "depart": w.depart, "return": w.ret,
            "depart_until": w.depart_until, "trip_days": w.trip_days,
            "flights": list(w.flights) if w.flights else None, "target": w.target,
            "search": w.search_id, "link": tracker.google_link(w),
            "state": {k: st.get(k) for k in ("last", "last_flights", "low", "low_at", "high",
                                              "first", "first_at", "misses", "last_seen_at")},
            "points": points,
            "previous_price": prev,
            "current": {"price": current.price, "flights": list(current.flights), "legs": list(current.legs)}
            if current else None,
            "choices": [{"price": o.price, "flights": list(o.flights), "legs": list(o.legs)} for o in fitting[:6]]
            if w.flights and tracker.ANY in w.flights else [],
            "checked_at": (state.get("searches", {}).get(w.search_id) or {}).get("checked_at"),
        })
        end = w.ret or w.depart_until or w.depart
        t = trips.setdefault(w.trip, {"name": w.trip, "start": w.depart, "end": end, "watches": []})
        t["start"] = min(t["start"], w.depart)
        t["end"] = max(t["end"], end)
        t["watches"].append(w.id)

    known = {w.id for w in watches}
    return {
        "generated_at": datetime.now(tracker.IST).replace(microsecond=0).isoformat(),
        "updated_at": state.get("updated_at"),
        "failed_runs": state.get("failed_runs", 0),
        "check_every_minutes": settings["check_every_minutes"],
        "rules": {k: settings[k] for k in ("drop_alert_rupees", "drop_alert_percent",
                                           "rise_alert_rupees", "rise_alert_percent", "direct_only")},
        "trips": sorted(trips.values(), key=lambda t: t["start"]),
        "watches": out_watches,
        "searches": latest.get("searches", {}),
        "events": [e for e in events if e["watch_id"] in known and e["kind"] != "baseline"][-200:],
        "airlines": tracker.AIRLINE_NAMES,
        "demo": False,
    }


NOSCRIPT_MARKER = "<!--__NOSCRIPT__-->"


def static_summary(data: dict) -> str:
    """Plain HTML with every current price, for viewers that do not run JavaScript
    (some phone file previewers). The full page replaces it when scripts run."""
    esc = html_lib.escape
    by_id = {w["id"]: w for w in data["watches"]}
    parts = ['<div class="card"><p><strong>Charts need a web browser.</strong> '
             "If this page opened in a file preview, open it in Chrome, Safari or Brave instead. "
             "Current prices:</p>"]
    if data.get("updated_at"):
        parts.append(f'<p class="muted small">Last check: {esc(data["updated_at"][:16].replace("T", " "))} IST</p>')
    for trip in data["trips"]:
        parts.append(f"<h3>{esc(trip['name'])}</h3><table class=\"data\"><tr><th>Watch</th>"
                     "<th>Now</th><th>Low</th><th>High</th></tr>")
        for wid in trip["watches"]:
            w = by_id[wid]; st = w["state"]
            def f(v):
                return tracker.inr(v) if v is not None else "–"
            flights = " + ".join(st.get("last_flights") or [])
            parts.append(f"<tr><td>{esc(w['name'])}<br><span class=\"muted small\">{esc(flights)}</span></td>"
                         f"<td>{f(st.get('last'))}</td><td>{f(st.get('low'))}</td><td>{f(st.get('high'))}</td></tr>")
        parts.append("</table>")
    parts.append("</div>")
    return "\n".join(parts)


def render(data: dict) -> str:
    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace(NOSCRIPT_MARKER, "<noscript>" + static_summary(data) + "</noscript>")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # Keep the JSON from ever closing the <script> tag or opening a comment early.
    payload = payload.replace("<", "\\u003c")
    if html.count(DATA_MARKER) != 1:
        raise RuntimeError("dashboard template is missing its data marker")
    return html.replace(DATA_MARKER, payload)


def build(config: Path, store, out: Path, demo: bool = False) -> Path:
    data = demo_data(config) if demo else collect(config, store)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(render(data), encoding="utf-8")
    tmp.replace(out)
    return out


# --------------------------------------------------------------------------
# Demo data: made-up history so the page can be previewed before real checks
# --------------------------------------------------------------------------

DEMO_START_PRICES = {  # roughly the real prices seen on 20 Nov 2026
    "6E 1457/6E 1458": 27338, "6E 1203/6E 1204": 30798, "6E 1247/6E 1248": 26576,
    "6E 1207/6E 1208": 28041, "6E 1217/6E 1218": 39431, "QP 623/QP 624": 45727,
    "6E 1217/IX 931": 39346, "QP 623": 19838, "6E 1217": 18853, "QP 624": 25926, "IX 931": 20493,
}
DEMO_TIMES = {"6E 1457": ("10:20", "17:40", 290), "6E 1458": ("19:30", "21:40", 280),
              "6E 1203": ("09:20", "17:45", 355), "6E 1204": ("19:40", "23:10", 360),
              "6E 1247": ("20:00", "03:10", 280), "6E 1248": ("04:20", "06:20", 270),
              "6E 1207": ("19:15", "03:30", 345), "6E 1208": ("04:25", "07:35", 340),
              "6E 1217": ("06:20", "11:30", 220), "6E 1218": ("12:35", "14:35", 210),
              "QP 623": ("10:40", "15:50", 220), "QP 624": ("17:00", "19:10", 220),
              "IX 931": ("12:40", "14:50", 220), "SQ 509": ("11:40", "18:50", 280),
              "AI 2381": ("08:00", "16:10", 340)}


def _demo_legs(flights, w):
    legs = []
    for i, code in enumerate(flights):
        dep_t, arr_t, minutes = DEMO_TIMES.get(code, ("12:00", "16:00", 240))
        day = w.depart if i == 0 else w.ret
        frm, to = (w.origin, w.destination) if i == 0 else (w.destination, w.origin)
        arr_day = day
        if arr_t < dep_t:
            arr_day = (datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
        legs.append({"flight": code, "airline": tracker.AIRLINE_NAMES.get(code.split()[0], ""),
                     "from": frm, "to": to, "dep": f"{day}T{dep_t}", "arr": f"{arr_day}T{arr_t}",
                     "minutes": minutes})
    return legs


DEMO_EXTRA_OPTIONS = {  # other direct flights Google would list, (flights, today's price)
    ("BLR", "SIN", True): [(("6E 1247", "6E 1248"), 26576), (("SQ 509", "6E 1458"), 31840),
                           (("AI 2381", "6E 1458"), 33410)],
    ("DEL", "SIN", True): [(("6E 1207", "6E 1208"), 28041)],
    ("BLR", "HKT", True): [(("6E 1217", "IX 931"), 39089), (("6E 1217", "6E 1218"), 42850),
                           (("6E 1217", "QP 624"), 43964), (("QP 623", "IX 931"), 36661)],
    ("BLR", "HKT", False): [(("6E 1217",), 18853)],
    ("HKT", "BLR", False): [(("IX 931",), 20493)],
}


def _walk(rng, n, base, shared):
    """Price path ending at `base`, built backwards: a route-wide factor plus
    the flight's own drift and occasional fare-bucket jumps."""
    own, out = 0.0, []
    for i in range(n):
        out.append(round(base * (1 + shared[i] + own) / 10) * 10 if i else base)
        r = rng.random()
        own *= 0.97
        if r < 0.025:
            own += rng.uniform(0.04, 0.10)   # going back in time: fares were higher
        elif r < 0.035:
            own -= rng.uniform(0.03, 0.07)
        elif r < 0.25:
            own += rng.gauss(0, 0.006)
        own = max(-0.12, min(0.25, own))
    out.reverse()
    return out


def demo_data(config: Path) -> dict:
    settings, watches = tracker.load_config(config)
    rng = random.Random(11)
    now = datetime.now(tracker.IST).replace(minute=23, second=0, microsecond=0)
    checks = [now - timedelta(hours=2 * i) for i in range(30 * 12, -1, -1)]  # 30 days, every 2 h
    n = len(checks)

    # Price paths for every option of every search.
    paths: dict[str, dict[tuple, list[float]]] = {}
    for w in watches:
        if w.search_id in paths:
            continue
        shared, x = [], 0.0
        for i in range(n):  # route-wide trend, backwards: a bit dearer a month ago
            shared.append(x)
            x = 0.985 * x + rng.gauss(0.0012, 0.004)
        if w.flexible:  # one option per departure date (and return date)
            options = {}
            day, end = datetime.fromisoformat(w.depart), datetime.fromisoformat(w.depart_until)
            base = 26576 if w.trip_days else 13340
            while day <= end:
                dates = (day.date().isoformat(),)
                if w.trip_days:
                    dates += ((day + timedelta(days=w.trip_days)).date().isoformat(),)
                weekend = day.weekday() >= 4
                options[dates] = round(base * rng.uniform(1.0, 1.35) * (1.08 if weekend else 1.0) / 10) * 10
                day += timedelta(days=1)
            paths[w.search_id] = {fl: _walk(rng, n, price, shared) for fl, price in options.items()}
            continue
        options = {o[0]: o[1] for o in DEMO_EXTRA_OPTIONS.get((w.origin, w.destination, bool(w.ret)), [])}
        for other in watches:
            if other.search_id == w.search_id and other.flights and tracker.ANY not in other.flights:
                options.setdefault(other.flights, DEMO_START_PRICES.get("/".join(other.flights), 25000))
        paths[w.search_id] = {fl: _walk(rng, n, price, shared) for fl, price in options.items()}
    # Akasa round trip vanishes for 8 hours, to show a "not found" gap.
    for sid, opts in paths.items():
        for fl in opts:
            if fl[0].startswith("QP") and len(fl) == 2:
                for i in range(200, 204):
                    opts[fl][i] = None

    state, history, events, searches = {}, [], [], {}
    for w in watches:
        opts = paths[w.search_id]
        st = None
        for i, ts in enumerate(checks):
            if w.flights and tracker.ANY not in w.flights:
                pr, fl = opts.get(w.flights, [None] * n)[i], w.flights
            else:
                avail = [(p[i], f) for f, p in opts.items()
                         if p[i] is not None and (not w.flights or tracker.matches(w.flights, f))]
                pr, fl = min(avail) if avail else (None, None)
            stamp = ts.isoformat()
            st, evs = tracker.evaluate(st, pr, fl if pr else None, w.target, settings, stamp)
            history.append({"checked_at": stamp, "watch_id": w.id, "price": f"{pr:.0f}" if pr else "",
                            "flights": " / ".join(fl) if pr else "", "status": "ok" if pr else "not_found"})
            for kind, info in evs:
                if kind != "baseline":
                    events.append({"checked_at": stamp, "watch_id": w.id, "kind": kind,
                                   "price": f"{st['last']:.0f}" if kind != "missing" else "",
                                   "reference": f"{info['ref']:.0f}" if "ref" in info else ""})
        state[w.id] = st
    for w in watches:
        if w.search_id not in searches:
            searches[w.search_id] = {"checked_at": now.isoformat(), "options": sorted(
                ({"price": float(p[-1]), "flights": list(fl), "legs": [] if w.flexible else _demo_legs(fl, w)}
                 for fl, p in paths[w.search_id].items() if p[-1] is not None),
                key=lambda o: o["price"])}
    events.sort(key=lambda e: e["checked_at"])

    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        fs = FileStore(Path(tmp))
        fs.save_state({"watches": state, "failed_runs": 0, "updated_at": now.isoformat()})
        fs.add_history(history)
        fs.add_events(events)
        fs.save_latest({"updated_at": now.isoformat(), "searches": searches})
        data = collect(config, fs)
    data["demo"] = True
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(HERE / "watches.toml"))
    parser.add_argument("--data-dir", default=str(HERE / "data"))
    parser.add_argument("--out", default=None, help="output file (default: dashboard/index.html, "
                                                    "or dashboard/demo.html with --demo)")
    parser.add_argument("--demo", action="store_true", help="use made-up history (for previewing)")
    args = parser.parse_args(argv)
    out = Path(args.out) if args.out else HERE / "dashboard" / ("demo.html" if args.demo else "index.html")
    build(Path(args.config), FileStore(Path(args.data_dir)), out, demo=args.demo)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
