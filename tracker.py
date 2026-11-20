#!/usr/bin/env python3
"""Direct-flight price tracker with Telegram / email alerts.

Reads watches.toml, asks Google Flights (through the open-source `flights`
library, github.com/punitarani/fli) for current direct-flight prices, saves
every price in data/, and sends an alert when a price drops, reaches your
target, climbs fast, or a watched flight disappears.

Usage:
  python tracker.py                 check prices and send alerts
  python tracker.py --summary       also send a summary of every watch
  python tracker.py --dry-run       check and print only; send and save nothing
  python tracker.py --test-notify   send a test message to check your setup

Alert channels are configured with environment variables (GitHub secrets):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  EMAIL_TO, EMAIL_PASSWORD (+ optional EMAIL_FROM, EMAIL_USERNAME,
  SMTP_HOST [smtp.gmail.com], SMTP_PORT [465])
With none set, alerts are only printed.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import smtplib
import sys
import time
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
IST = timezone(timedelta(hours=5, minutes=30))

# Google sometimes answers with a short error instead of results (about one
# request in three in testing), so every search is retried.
SEARCH_ATTEMPTS = 5
RETRY_WAIT_SECONDS = (3, 6, 10, 15)
PAUSE_BETWEEN_SEARCHES = 2
# Round trips: how many outbound flights to price returns for. Direct routes
# rarely have more than a handful of flights a day.
ROUND_TRIP_OUTBOUND_OPTIONS = 8
# Checks in a row with no prices at all before a "tracker broken" alert.
BROKEN_RUNS_ALERT = 3

AIRLINE_NAMES = {
    "6E": "IndiGo", "QP": "Akasa Air", "AI": "Air India", "IX": "Air India Express",
    "SG": "SpiceJet", "UK": "Vistara", "SQ": "Singapore Airlines", "TR": "Scoot",
    "TG": "Thai Airways", "FD": "Thai AirAsia", "AK": "AirAsia", "MH": "Malaysia Airlines",
    "EK": "Emirates", "FZ": "flydubai", "EY": "Etihad", "QR": "Qatar Airways",
    "G9": "Air Arabia", "WY": "Oman Air", "UL": "SriLankan", "VN": "Vietnam Airlines",
    "VJ": "VietJet", "CX": "Cathay Pacific", "BA": "British Airways", "LH": "Lufthansa",
}

DEFAULT_SETTINGS = {
    "currency": "INR",
    "adults": 1,
    "direct_only": True,
    "drop_alert_rupees": 500,
    "drop_alert_percent": 3,
    "rise_alerts": True,
    "rise_alert_rupees": 1000,
    "rise_alert_percent": 10,
    "missing_checks_alert": 3,
}


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Watch:
    id: str
    name: str
    origin: str
    destination: str
    depart: str
    ret: str | None
    flights: tuple[str, ...] | None  # None = cheapest direct option
    target: float | None

    @property
    def search_key(self) -> tuple:
        return (self.origin, self.destination, self.depart, self.ret)


@dataclass(frozen=True)
class Option:
    price: float
    flights: tuple[str, ...]
    times: tuple[str, ...]


class ConfigError(Exception):
    pass


def normalize_flight(text: str) -> str:
    """'6e1457', '6E-1457', '6E 01457' -> '6E 1457'."""
    m = re.fullmatch(r"\s*([A-Za-z0-9]{2})\s*-?\s*(\d{1,5})\s*", str(text))
    if not m:
        raise ConfigError(f"flight {text!r} should look like '6E 1457'")
    return f"{m.group(1).upper()} {int(m.group(2))}"


def _date_text(value, field: str, name: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as e:
        raise ConfigError(f"watch {name!r}: {field} {value!r} is not a YYYY-MM-DD date") from e


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def load_config(path: Path) -> tuple[dict, list[Watch]]:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    settings = dict(DEFAULT_SETTINGS)
    for key, value in raw.get("settings", {}).items():
        if key not in DEFAULT_SETTINGS:
            raise ConfigError(f"unknown setting {key!r} (known: {', '.join(DEFAULT_SETTINGS)})")
        settings[key] = value

    watches, seen = [], set()
    for i, w in enumerate(raw.get("watch", []), 1):
        name = str(w.get("name") or f"watch {i}")
        wid = slugify(name)
        if wid in seen:
            raise ConfigError(f"two watches are named {name!r}; names must differ")
        seen.add(wid)
        try:
            origin, destination = str(w["from"]).upper(), str(w["to"]).upper()
            depart = _date_text(w["depart"], "depart", name)
        except KeyError as e:
            raise ConfigError(f"watch {name!r} is missing {e.args[0]!r}") from e
        for code in (origin, destination):
            if not re.fullmatch(r"[A-Z]{3}", code):
                raise ConfigError(f"watch {name!r}: {code!r} is not a 3-letter airport code")
        ret = _date_text(w["return"], "return", name) if w.get("return") else None
        if ret and ret < depart:
            raise ConfigError(f"watch {name!r}: return is before depart")
        flights = None
        if w.get("flights"):
            flights = tuple(normalize_flight(f) for f in w["flights"])
            legs = 2 if ret else 1
            if len(flights) != legs:
                raise ConfigError(
                    f"watch {name!r}: list {legs} flight(s) (one per direction), got {len(flights)}")
        target = float(w["target"]) if w.get("target") else None
        watches.append(Watch(wid, name, origin, destination, depart, ret, flights, target))
    return settings, watches


# --------------------------------------------------------------------------
# Searching (the only part that touches Google Flights)
# --------------------------------------------------------------------------

def _flight_code(leg) -> str:
    airline = getattr(leg.airline, "name", str(leg.airline)).removeprefix("_")
    return f"{airline} {int(leg.flight_number) if str(leg.flight_number).isdigit() else leg.flight_number}"


def _search_once(key: tuple, settings: dict) -> list[Option]:
    from fli.core.builders import build_flight_segments
    from fli.models import Airport, FlightSearchFilters, MaxStops, PassengerInfo, SortBy
    from fli.search import SearchFlights

    origin, destination, depart, ret = key
    try:
        segments, trip_type = build_flight_segments(Airport[origin], Airport[destination], depart, ret)
    except KeyError as e:
        raise ConfigError(f"airport {e.args[0]} is not known to the flights library") from e
    filters = FlightSearchFilters(
        trip_type=trip_type,
        passenger_info=PassengerInfo(adults=int(settings["adults"])),
        flight_segments=segments,
        stops=MaxStops.NON_STOP if settings["direct_only"] else MaxStops.ANY,
        sort_by=SortBy.CHEAPEST,
    )
    results = SearchFlights().search(
        filters, top_n=ROUND_TRIP_OUTBOUND_OPTIONS,
        currency=settings["currency"], country="IN", language="en",
    ) or []
    options = []
    for result in results:
        parts = result if isinstance(result, tuple) else (result,)
        # Round-trip rows carry the total trip price on the last part.
        price = next((p.price for p in reversed(parts) if p.price is not None), None)
        if price is None:
            continue
        if settings["direct_only"] and any(p.stops for p in parts):
            continue
        legs = [leg for p in parts for leg in p.legs]
        options.append(Option(
            price=float(price),
            flights=tuple(_flight_code(leg) for leg in legs),
            times=tuple(leg.departure_datetime.strftime("%-d %b %H:%M") for leg in legs),
        ))
    return options


def fetch_options(key: tuple, settings: dict, needed: set[tuple], search=_search_once,
                  sleep=time.sleep) -> list[Option] | None:
    """All options for one search, retrying Google's random errors.

    Stops as soon as there are results and every exact flight combination in
    `needed` has been seen. Returns None if no attempt gave any result.
    """
    merged: dict[tuple, Option] = {}
    for attempt in range(SEARCH_ATTEMPTS):
        try:
            options = search(key, settings)
        except ConfigError:
            raise
        except Exception as e:  # network trouble or a changed response format
            log(f"    attempt {attempt + 1}: {type(e).__name__}: {e}")
            options = []
        for o in options:
            if o.flights not in merged or o.price < merged[o.flights].price:
                merged[o.flights] = o
        if merged and needed <= merged.keys():
            break
        if attempt < SEARCH_ATTEMPTS - 1:
            sleep(RETRY_WAIT_SECONDS[min(attempt, len(RETRY_WAIT_SECONDS) - 1)])
    return sorted(merged.values(), key=lambda o: (o.price, o.times)) or None


def pick(watch: Watch, options: list[Option]) -> Option | None:
    if watch.flights:
        return next((o for o in options if o.flights == watch.flights), None)
    return min(options, key=lambda o: (o.price, o.times)) if options else None


# --------------------------------------------------------------------------
# Alert rules (pure, unit-tested)
# --------------------------------------------------------------------------

def evaluate(prev: dict | None, price: float | None, flights: tuple[str, ...] | None,
             target: float | None, s: dict, now: str) -> tuple[dict, list[tuple[str, dict]]]:
    """Update one watch's state with a new price and return the alert events.

    `ref` is the price at the last alert (or the first check). Drops and rises
    are measured against it, so a slow slide still alerts once it adds up, and
    each alert resets the reference.
    """
    st = dict(prev or {})
    events: list[tuple[str, dict]] = []
    if price is None:
        st["misses"] = st.get("misses", 0) + 1
        if st["misses"] == s["missing_checks_alert"]:
            events.append(("missing", {}))
        return st, events

    was_missing = st.get("misses", 0) >= s["missing_checks_alert"]
    st["misses"] = 0
    if "last" not in st:
        st.update(first=price, first_at=now, low=price, low_at=now, high=price, ref=price)
        events.append(("baseline", {}))
        if target and price <= target:
            events.append(("target", {}))
    else:
        ref = st.get("ref", st["last"])
        new_low = price < st["low"]
        drop_needed = max(s["drop_alert_rupees"], ref * s["drop_alert_percent"] / 100)
        rise_needed = max(s["rise_alert_rupees"], ref * s["rise_alert_percent"] / 100)
        if price <= ref - drop_needed:
            events.append(("drop", {"ref": ref, "new_low": new_low}))
            st["ref"] = price
        elif s["rise_alerts"] and price >= ref + rise_needed:
            events.append(("rise", {"ref": ref}))
            st["ref"] = price
        if target and price <= target < st["last"]:
            events.append(("target", {}))
        if was_missing:
            events.append(("back", {}))
        if new_low:
            st["low"], st["low_at"] = price, now
        st["high"] = max(st.get("high", price), price)
    st["last"] = price
    st["last_flights"] = list(flights or ())
    st["last_seen_at"] = now
    return st, events


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------

def inr(amount: float) -> str:
    """Indian digit grouping: 123456 -> ₹1,23,456."""
    n = int(round(amount))
    sign, n = ("-" if n < 0 else ""), abs(n)
    s = str(n)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = ",".join(re.findall(r"\d{1,2}", head[::-1]))[::-1]
        s = f"{head},{tail}"
    return f"{sign}₹{s}"


def google_link(w: Watch) -> str:
    q = f"Flights from {w.origin} to {w.destination} on {w.depart}"
    if w.ret:
        q += f" through {w.ret}"
    q += " nonstop"
    return ("https://www.google.com/travel/flights?"
            + urllib.parse.urlencode({"q": q, "curr": "INR", "hl": "en", "gl": "IN"}))


def describe_flights(flights, times) -> str:
    parts = []
    for code, t in zip(flights or (), times or ()):
        airline = AIRLINE_NAMES.get(code.split()[0], "")
        parts.append(f"{code}{' (' + airline + ')' if airline else ''} {t}")
    return " / ".join(parts) or "-"


def event_message(kind: str, info: dict, w: Watch, st: dict, option: Option | None) -> str:
    price = st.get("last")
    flights = describe_flights(option.flights, option.times) if option else ", ".join(w.flights or ())
    extra = f"Lowest seen {inr(st['low'])}" if "low" in st else ""
    if w.target:
        extra += f" · target {inr(w.target)}"
    if kind == "drop":
        diff = info["ref"] - price
        head = f"🔻 PRICE DROP{' (new low)' if info.get('new_low') else ''}: {w.name}"
        body = f"{inr(price)}, down {inr(diff)} ({diff / info['ref']:.1%}) from {inr(info['ref'])}"
    elif kind == "rise":
        diff = price - info["ref"]
        head = f"🔺 RISING FAST: {w.name}"
        body = f"{inr(price)}, up {inr(diff)} ({diff / info['ref']:.1%}) from {inr(info['ref'])}"
    elif kind == "target":
        head = f"🎯 TARGET REACHED: {w.name}"
        body = f"{inr(price)} (your target {inr(w.target)})"
    elif kind == "back":
        head = f"✅ BACK: {w.name}"
        body = f"Available again at {inr(price)}"
    elif kind == "missing":
        head = f"⚠️ NOT FOUND: {w.name}"
        body = (f"{', '.join(w.flights or ())} has not appeared in the last {st.get('misses')} checks "
                "(sold out, schedule change, or no longer sold as a direct flight).")
        return "\n".join([head, body, google_link(w)])
    else:
        return ""
    return "\n".join([head, body, flights, extra, google_link(w)])


def price_24h_ago(history: list[dict], wid: str, now: datetime) -> float | None:
    cutoff = (now - timedelta(hours=24)).isoformat()
    best = None
    for row in history:
        if row["watch_id"] == wid and row["price"] and row["checked_at"] <= cutoff:
            best = float(row["price"])
    return best


def summary_message(watches, state, history, now, title) -> str:
    lines = [title, ""]
    for w in watches:
        st = state.get(w.id, {})
        lines.append(w.name)
        if "last" not in st:
            lines.append("  no price yet")
            continue
        change = ""
        old = price_24h_ago(history, w.id, now)
        if old:
            d = st["last"] - old
            change = " (no change in 24 h)" if abs(d) < 1 else f" ({'+' if d > 0 else '-'}{inr(abs(d))} in 24 h)"
        status = "  NOT FOUND on the last check" if st.get("misses") else ""
        lines.append(f"  {inr(st['last'])}{change} · low {inr(st['low'])} · high {inr(st['high'])}{status}")
        if not w.flights and st.get("last_flights"):
            lines.append(f"  cheapest: {' / '.join(st['last_flights'])}")
    return "\n".join(lines).rstrip()


# --------------------------------------------------------------------------
# Alert channels
# --------------------------------------------------------------------------

def _telegram(token: str, chat_id: str, text: str) -> None:
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > 3900:
            chunks.append(current)
            current = ""
        current += line
    chunks.append(current)
    for chunk in chunks:
        data = urllib.parse.urlencode({
            "chat_id": chat_id, "text": chunk, "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Telegram answered HTTP {resp.status}")


def _email(text: str) -> None:
    to = os.environ["EMAIL_TO"]
    sender = os.environ.get("EMAIL_FROM") or os.environ.get("EMAIL_USERNAME") or to
    username = os.environ.get("EMAIL_USERNAME") or sender
    msg = EmailMessage()
    msg["Subject"] = text.splitlines()[0][:120]
    msg["From"], msg["To"] = sender, to
    msg.set_content(text)
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
        smtp.login(username, os.environ["EMAIL_PASSWORD"])
        smtp.send_message(msg)


def send(text: str, dry_run: bool) -> bool:
    """Print the message and send it to every configured channel."""
    print("\n----- message -----\n" + text + "\n-------------------")
    if dry_run:
        return True
    ok, used = True, False
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        used = True
        try:
            _telegram(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"], text)
            log("  sent to Telegram")
        except Exception as e:
            ok = False
            log(f"  Telegram failed: {type(e).__name__}: {e}")
    if os.environ.get("EMAIL_TO") and os.environ.get("EMAIL_PASSWORD"):
        used = True
        try:
            _email(text)
            log("  sent by email")
        except Exception as e:
            ok = False
            log(f"  email failed: {type(e).__name__}: {e}")
    if not used:
        log("  no alert channel configured (set TELEGRAM_* or EMAIL_* secrets); printed only")
    return ok


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

HISTORY_FIELDS = ["checked_at", "watch_id", "price", "flights", "status"]


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"watches": {}, "failed_runs": 0}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_history(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def append_history(path: Path, rows: list[dict]) -> None:
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=HISTORY_FIELDS)
        if new:
            writer.writeheader()
        writer.writerows(rows)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def run(args) -> int:
    settings, watches = load_config(Path(args.config))
    if args.test_notify:
        ok = send("✈️ Test message from your flight tracker. Price alerts will arrive here.", False)
        return 0 if ok else 1

    data_dir = Path(args.data_dir)
    state_path, history_path = data_dir / "state.json", data_dir / "history.csv"
    state = load_state(state_path)
    history = load_history(history_path)
    now_dt = datetime.now(IST).replace(microsecond=0)
    now = now_dt.isoformat()

    active = [w for w in watches if w.depart >= now_dt.date().isoformat()]
    for w in watches:
        if w not in active:
            log(f"Skipping (departure has passed): {w.name}")
    if not active:
        log("No active watches.")
        return 0

    needed: dict[tuple, set[tuple]] = {}
    for w in active:
        needed.setdefault(w.search_key, set())
        if w.flights:
            needed[w.search_key].add(w.flights)

    results: dict[tuple, list[Option] | None] = {}
    for i, key in enumerate(needed):
        origin, destination, depart, ret = key
        log(f"Searching direct {origin}->{destination} {depart}" + (f" return {ret}" if ret else ""))
        if i:
            time.sleep(PAUSE_BETWEEN_SEARCHES)
        results[key] = fetch_options(key, settings, needed[key])
        n = len(results[key] or [])
        log(f"  {n} direct option(s)" if n else "  no answer from Google after retries")

    messages, history_rows, exit_code = [], [], 0
    wstate = state.setdefault("watches", {})
    if not any(results.values()):
        state["failed_runs"] = state.get("failed_runs", 0) + 1
        log(f"No prices at all this check ({state['failed_runs']} in a row).")
        if state["failed_runs"] == BROKEN_RUNS_ALERT:
            messages.append(
                f"🛑 Flight tracker: no prices from Google Flights for {BROKEN_RUNS_ALERT} checks in a row. "
                "Google may have changed something; the 'flights' library may need an update "
                "(edit requirements.txt). Your Google Flights alerts still work meanwhile.")
        if state["failed_runs"] >= BROKEN_RUNS_ALERT:
            exit_code = 2
    else:
        state["failed_runs"] = 0

    first_run = False
    log("")
    for w in active:
        options = results.get(w.search_key)
        if options is None:
            continue  # Google did not answer: not counted as "flight missing"
        option = pick(w, options)
        st, events = evaluate(wstate.get(w.id), option.price if option else None,
                              option.flights if option else None, w.target, settings, now)
        wstate[w.id] = st
        history_rows.append({
            "checked_at": now, "watch_id": w.id,
            "price": f"{option.price:.0f}" if option else "",
            "flights": " / ".join(option.flights) if option else "",
            "status": "ok" if option else "not_found"})
        shown = f"{inr(option.price)}  {describe_flights(option.flights, option.times)}" if option else "NOT FOUND"
        log(f"{w.name}\n    {shown}")
        for kind, info in events:
            if kind == "baseline":
                first_run = True
                continue
            messages.append(event_message(kind, info, w, st, option))

    known = {w.id for w in watches}
    for wid in [k for k in wstate if k not in known]:
        del wstate[wid]  # watch removed from watches.toml

    stamp = now_dt.strftime("%-d %b %Y, %H:%M IST")
    if first_run or args.summary:
        title = "✈️ Flight tracker started. Current direct prices:" if first_run and not args.summary \
            else f"✈️ Daily flight price summary ({stamp})"
        messages.append(summary_message(active, wstate, history, now_dt, title))

    if messages and not send("\n\n".join(messages), args.dry_run):
        exit_code = exit_code or 1
    if not messages:
        log("\nNo alerts this time.")

    if args.dry_run:
        log("\n(dry run: nothing saved)")
    else:
        state["updated_at"] = now
        save_state(state_path, state)
        if history_rows:
            append_history(history_path, history_rows)
    return exit_code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(HERE / "watches.toml"), help="watch list (default: watches.toml)")
    parser.add_argument("--data-dir", default=str(HERE / "data"), help="where prices are saved (default: data/)")
    parser.add_argument("--summary", action="store_true", help="also send a summary of every watch")
    parser.add_argument("--dry-run", action="store_true", help="print only; send and save nothing")
    parser.add_argument("--test-notify", action="store_true", help="send a test message and exit")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except ConfigError as e:
        log(f"Config problem: {e}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
