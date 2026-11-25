#!/usr/bin/env python3
"""Direct-flight price tracker with Telegram / email alerts.

Reads watches.toml, asks Google Flights (through the open-source `flights`
library, github.com/punitarani/fli) for current direct-flight prices, saves
every price in data/, and sends an alert when a price drops, reaches your
target, climbs fast, or a watched flight disappears.

Usage:
  python tracker.py                 check the watches that are due, send alerts
  python tracker.py --force         check every watch now
  python tracker.py --summary       also send a summary of every watch
  python tracker.py --dry-run       check and print only; send and save nothing
  python tracker.py --test-notify   send a test message to check your setup

On Vercel the same check runs from app.py (see README.md); prices are stored
in Upstash Redis there and in data/ everywhere else (store.py).

Alert channels are configured with environment variables (GitHub secrets):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  EMAIL_TO, EMAIL_PASSWORD (+ optional EMAIL_FROM, EMAIL_USERNAME,
  SMTP_HOST [smtp.gmail.com], SMTP_PORT [465])
With none set, alerts are only printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import time
import tomllib
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from store import EVENT_FIELDS, HISTORY_FIELDS, FileStore, open_store  # noqa: F401

HERE = Path(__file__).resolve().parent
# The flights library waits up to 60 s for a Google request by default; healthy
# answers take under a second, so give up on a stuck request much sooner.
os.environ.setdefault("FLI_TIMEOUT", "6")
IST = timezone(timedelta(hours=5, minutes=30))

# Google answers about half of all requests with a short error, at random
# (measured from this computer and from Vercel), so every request is retried
# quickly: waiting longer does not help.
SEARCH_ATTEMPTS = 6
RETRY_WAIT_SECONDS = (1, 1.5, 2, 3, 5)
PAUSE_BETWEEN_SEARCHES = 1
# Round trips: Google's outbound list already shows each outbound flight's
# cheapest round-trip price, so returns are priced only for the watched
# outbound flights plus this many of the cheapest ones.
ROUND_TRIP_CHEAPEST_OUTBOUND = 2
ROUND_TRIP_OUTBOUND_OPTIONS = 8  # fallback path (library's own expansion)
# Full re-searches of a round trip when a watched pair is still missing
# (each request inside is already retried).
ROUND_TRIP_ATTEMPTS = 2
# After this many seconds a run stops starting new searches and retrying, so a
# bad day at Google cannot run up the host's limits. Whatever was found is saved.
RUN_BUDGET_SECONDS = 8 * 60
_deadline: float | None = None


def out_of_time() -> bool:
    return _deadline is not None and time.monotonic() > _deadline


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
    "send_dashboard": True,  # attach the dashboard file to summaries (file mode only)
    "check_every_minutes": 30,  # how often each watch is refreshed
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
    trip: str = ""  # groups watches on the dashboard
    # Flexible dates: cheapest day between depart and depart_until (one way),
    # or cheapest trip of trip_days days starting in that window (round trip).
    depart_until: str | None = None
    trip_days: int | None = None

    @property
    def flexible(self) -> bool:
        return self.depart_until is not None

    @property
    def last_date(self) -> str:
        """The last day this watch could depart (it stops after this)."""
        return self.depart_until or self.depart

    @property
    def search_id(self) -> str:
        return search_id(self.search_key)

    @property
    def search_key(self) -> tuple:
        if self.flexible:
            return (self.origin, self.destination, f"{self.depart}..{self.depart_until}",
                    f"{self.trip_days}d" if self.trip_days else None)
        return (self.origin, self.destination, self.depart, self.ret)


@dataclass(frozen=True)
class Option:
    price: float
    flights: tuple[str, ...]
    times: tuple[str, ...]
    # Per leg: {"flight", "airline", "from", "to", "dep", "arr", "minutes"}
    # (local airport times, ISO format). Not used for comparisons.
    legs: tuple = field(default=(), compare=False)


def search_id(key: tuple) -> str:
    origin, destination, depart, ret = key
    return "|".join([origin, destination, depart, ret or ""])


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


def default_trip(origin: str, destination: str, depart: str, ret: str | None) -> str:
    a, b = sorted((origin, destination))
    return f"{a} - {b}, {depart}" + (f" to {ret}" if ret else "")


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
        until = _date_text(w["depart_until"], "depart_until", name) if w.get("depart_until") else None
        trip_days = int(w["trip_days"]) if w.get("trip_days") else None
        if until:
            if until < depart:
                raise ConfigError(f"watch {name!r}: depart_until is before depart")
            if ret or w.get("flights"):
                raise ConfigError(f"watch {name!r}: a flexible-date watch (depart_until) takes trip_days "
                                  "instead of return, and no flights")
            if trip_days is not None and not 1 <= trip_days <= 30:
                raise ConfigError(f"watch {name!r}: trip_days must be 1 to 30")
        elif trip_days:
            raise ConfigError(f"watch {name!r}: trip_days needs depart_until (a date window)")
        flights = None
        if w.get("flights"):
            flights = tuple(normalize_flight(f) for f in w["flights"])
            legs = 2 if ret else 1
            if len(flights) != legs:
                raise ConfigError(
                    f"watch {name!r}: list {legs} flight(s) (one per direction), got {len(flights)}")
        target = float(w["target"]) if w.get("target") else None
        trip = str(w.get("trip") or default_trip(origin, destination, depart, ret or until))
        watches.append(Watch(wid, name, origin, destination, depart, ret, flights, target, trip,
                             until, trip_days))
    return settings, watches


# --------------------------------------------------------------------------
# Searching (the only part that touches Google Flights)
# --------------------------------------------------------------------------

def _flight_code(leg) -> str:
    airline = getattr(leg.airline, "name", str(leg.airline)).removeprefix("_")
    return f"{airline} {int(leg.flight_number) if str(leg.flight_number).isdigit() else leg.flight_number}"


_library_tuned = False


def _tune_library() -> None:
    """Turn off the library's own hidden retries (3 tries, up to a minute each):
    this tracker retries itself, quickly and within its time budget."""
    global _library_tuned
    if _library_tuned:
        return
    _library_tuned = True
    try:
        from fli.search import client as fli_client
        from tenacity import stop_after_attempt
        for name in ("get", "post"):
            method = getattr(fli_client.Client, name)
            if hasattr(method, "retry_with"):
                setattr(fli_client.Client, name, method.retry_with(stop=stop_after_attempt(1)))
    except Exception as e:  # a library update changed this; the tracker still works, just slower
        log(f"(could not tune the flights library: {type(e).__name__})")


def _search_once(key: tuple, settings: dict, needed: set[tuple] = frozenset()) -> list[Option]:
    _tune_library()
    from fli.core.builders import build_flight_segments
    from fli.models import Airport, FlightSearchFilters, MaxStops, PassengerInfo, SortBy
    from fli.search import SearchFlights

    origin, destination, depart, ret = key
    if ".." in depart:
        return _search_dates(key, settings)
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
    client = SearchFlights()
    locale = dict(currency=settings["currency"], country="IN", language="en")
    if ret and hasattr(client, "_fetch_flights"):
        results = _round_trip(client, filters, locale, {f[0] for f in needed})
    else:
        results = client.search(filters, top_n=ROUND_TRIP_OUTBOUND_OPTIONS, **locale) or []
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
            legs=tuple({
                "flight": _flight_code(leg),
                "airline": AIRLINE_NAMES.get(_flight_code(leg).split()[0], ""),
                "from": getattr(leg.departure_airport, "name", str(leg.departure_airport)).removeprefix("_"),
                "to": getattr(leg.arrival_airport, "name", str(leg.arrival_airport)).removeprefix("_"),
                "dep": leg.departure_datetime.strftime("%Y-%m-%dT%H:%M"),
                "arr": leg.arrival_datetime.strftime("%Y-%m-%dT%H:%M"),
                "minutes": int(leg.duration),
            } for leg in legs),
        ))
    return options


def _search_dates(key: tuple, settings: dict) -> list[Option]:
    """Cheapest direct price for every departure day in a window.

    One "option" per day (or per day pair for round trips); its `flights` are
    the dates, because Google's date grid does not say which flight it is.
    """
    from fli.core.builders import build_date_search_segments
    from fli.models import Airport, DateSearchFilters, MaxStops, PassengerInfo
    from fli.search import SearchDates

    origin, destination, window, days = key
    start, end = window.split("..")
    trip_days = int(days[:-1]) if days else None
    try:
        segments, trip_type = build_date_search_segments(
            Airport[origin], Airport[destination], start, trip_duration=trip_days, is_round_trip=bool(trip_days))
    except KeyError as e:
        raise ConfigError(f"airport {e.args[0]} is not known to the flights library") from e
    filters = DateSearchFilters(
        trip_type=trip_type, passenger_info=PassengerInfo(adults=int(settings["adults"])),
        flight_segments=segments, stops=MaxStops.NON_STOP if settings["direct_only"] else MaxStops.ANY,
        from_date=start, to_date=end, duration=trip_days)
    rows = SearchDates().search(filters, currency=settings["currency"], country="IN", language="en") or []
    options = []
    for r in rows:
        if r.price is None:
            continue
        dates = tuple(d.strftime("%Y-%m-%d") for d in r.date)
        options.append(Option(price=float(r.price), flights=dates,
                              times=tuple(d.strftime("%a %-d %b") for d in r.date)))
    return options


def _with_retries(fn, what: str):
    """Call fn() until it returns results, waiting a little between tries."""
    for attempt in range(SEARCH_ATTEMPTS):
        if attempt and out_of_time():
            log(f"    {what}: run time budget used up, not retrying")
            return None
        try:
            result = fn()
        except Exception as e:
            log(f"    {what}, try {attempt + 1}: {type(e).__name__}: {e}")
            result = None
        if result:
            return result
        if out_of_time():
            log(f"    {what}: run time budget used up, not retrying")
            return None
        if attempt < SEARCH_ATTEMPTS - 1:
            time.sleep(RETRY_WAIT_SECONDS[min(attempt, len(RETRY_WAIT_SECONDS) - 1)])
    log(f"    {what}: no answer after {SEARCH_ATTEMPTS} tries")
    return None


def _round_trip(client, filters, locale, watched_outbound: set[str]) -> list[tuple]:
    """Price round trips one outbound flight at a time.

    The library prices returns for all outbound flights in parallel and
    silently drops any whose request hits Google's random error, so exact
    flight pairs could go "missing". Here each outbound flight's return
    prices are fetched on their own, with retries, and only for the watched
    outbound flights and the cheapest few (the outbound list's price is the
    cheapest round trip with that flight, so the cheapest pair is always found).
    """
    from copy import deepcopy

    outbound = _with_retries(lambda: client._fetch_flights(filters, capture_session=True, **locale),
                             "outbound list") or []
    def code(ob):
        return " + ".join(_flight_code(leg) for leg in ob.legs)
    by_price = sorted(outbound, key=lambda ob: ob.price if ob.price is not None else float("inf"))
    chosen = by_price[:ROUND_TRIP_CHEAPEST_OUTBOUND] + [ob for ob in by_price[ROUND_TRIP_CHEAPEST_OUTBOUND:]
                                                       if code(ob) in watched_outbound]
    combos = []
    for i, ob in enumerate(chosen):
        if i:
            time.sleep(1)
        sub = deepcopy(filters)
        sub.flight_segments[0].selected_flight = ob
        label = "returns for " + code(ob)
        returns = _with_retries(lambda: client._fetch_flights(sub, capture_session=False, **locale), label)
        combos.extend((ob, r) for r in returns or [])
    return combos


def fetch_options(key: tuple, settings: dict, needed: set[tuple], search=_search_once,
                  sleep=time.sleep) -> list[Option] | None:
    """All options for one search, retrying Google's random errors.

    Stops as soon as there are results and every exact flight combination in
    `needed` has been seen. Returns None if no attempt gave any result.
    """
    merged: dict[tuple, Option] = {}
    attempts = ROUND_TRIP_ATTEMPTS if key[3] else SEARCH_ATTEMPTS
    for attempt in range(attempts):
        if attempt and out_of_time():
            break
        try:
            options = search(key, settings, needed)
        except ConfigError:
            raise
        except Exception as e:  # network trouble or a changed response format
            log(f"    attempt {attempt + 1}: {type(e).__name__}: {e}")
            options = []
        if not options:
            log(f"    attempt {attempt + 1}: no results")
        for o in options:
            if o.flights not in merged or o.price < merged[o.flights].price:
                merged[o.flights] = o
        if merged and needed <= merged.keys():
            break
        if out_of_time():
            break
        if attempt < attempts - 1:
            sleep(RETRY_WAIT_SECONDS[min(attempt, len(RETRY_WAIT_SECONDS) - 1)])
    return sorted(merged.values(), key=lambda o: (o.price, o.times)) or None


def pick(watch: Watch, options: list[Option]) -> Option | None:
    if watch.flights:
        return next((o for o in options if o.flights == watch.flights), None)
    return min(options, key=lambda o: (o.price, o.flights if watch.flexible else o.times)) if options else None


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


def google_link(w: Watch, option: Option | None = None) -> str:
    depart, ret = w.depart, w.ret
    if w.flexible and option and option.flights:  # the cheapest dates found
        depart = option.flights[0]
        ret = option.flights[1] if len(option.flights) > 1 else None
    q = f"Flights from {w.origin} to {w.destination} on {depart}"
    if ret:
        q += f" through {ret}"
    q += " nonstop"
    return ("https://www.google.com/travel/flights?"
            + urllib.parse.urlencode({"q": q, "curr": "INR", "hl": "en", "gl": "IN"}))


def is_date(text: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(text)))


def describe_flights(flights, times) -> str:
    if flights and all(is_date(f) for f in flights):  # flexible-date result
        return "cheapest dates: " + " to ".join(times or flights)
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
    link = google_link(w, option)
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
        return "\n".join([head, body, link])
    else:
        return ""
    return "\n".join([head, body, flights, extra, link])


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
            if all(is_date(f) for f in st["last_flights"]):
                lines.append(f"  cheapest dates: {' to '.join(st['last_flights'])}")
            else:
                lines.append(f"  cheapest: {' / '.join(st['last_flights'])}")
    url = app_url()
    if url:
        lines += ["", f"Dashboard: {url}"]
    return "\n".join(lines).rstrip()


def app_url() -> str | None:
    """The web dashboard's address, when running on Vercel."""
    if os.environ.get("APP_URL"):
        return os.environ["APP_URL"].rstrip("/")
    if os.environ.get("VERCEL_PROJECT_PRODUCTION_URL"):
        return "https://" + os.environ["VERCEL_PROJECT_PRODUCTION_URL"]
    return None


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


def _telegram_document(token: str, chat_id: str, path: Path, caption: str) -> None:
    boundary = "----flighttracker" + os.urandom(8).hex()
    parts = []
    for name, value in (("chat_id", chat_id), ("caption", caption[:1000])):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{path.name}"\r\n'
                  "Content-Type: text/html\r\n\r\n").encode() + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument", data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Telegram answered HTTP {resp.status}")


def _email(text: str, attachment: Path | None = None) -> None:
    to = os.environ["EMAIL_TO"]
    sender = os.environ.get("EMAIL_FROM") or os.environ.get("EMAIL_USERNAME") or to
    username = os.environ.get("EMAIL_USERNAME") or sender
    msg = EmailMessage()
    msg["Subject"] = text.splitlines()[0][:120]
    msg["From"], msg["To"] = sender, to
    msg.set_content(text)
    if attachment:
        msg.add_attachment(attachment.read_bytes(), maintype="text", subtype="html", filename=attachment.name)
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
        smtp.login(username, os.environ["EMAIL_PASSWORD"])
        smtp.send_message(msg)


def send(text: str, dry_run: bool, attachment: Path | None = None) -> bool:
    """Print the message and send it (plus an optional file) to every configured channel."""
    print("\n----- message -----\n" + text + "\n-------------------")
    if attachment:
        print(f"(attached: {attachment.name})")
    if dry_run:
        return True
    ok, used = True, False
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        used = True
        try:
            _telegram(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"], text)
            if attachment:
                _telegram_document(os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"],
                                   attachment, "Price dashboard: open this file to see charts of every watch.")
            log("  sent to Telegram")
        except Exception as e:
            ok = False
            log(f"  Telegram failed: {type(e).__name__}: {e}")
    if os.environ.get("EMAIL_TO") and os.environ.get("EMAIL_PASSWORD"):
        used = True
        try:
            _email(text, attachment)
            log("  sent by email")
        except Exception as e:
            ok = False
            log(f"  email failed: {type(e).__name__}: {e}")
    if not used:
        log("  no alert channel configured (set TELEGRAM_* or EMAIL_* secrets); printed only")
    return ok


# --------------------------------------------------------------------------
# Storage helpers kept for scripts that use files directly
# --------------------------------------------------------------------------

def save_state(path: Path, state: dict) -> None:
    from store import _write_json_atomic
    _write_json_atomic(Path(path), state)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def log(msg: str) -> None:
    print(msg, flush=True)


def _due(key: tuple, searches: dict, now_dt: datetime, every_minutes: float, force: bool) -> bool:
    """A search is due when never checked, last check failed, or it is older
    than the check interval (minus 5 minutes of slack for scheduler jitter)."""
    info = searches.get(search_id(key))
    if force or not info or not info.get("ok"):
        return True
    age = (now_dt - datetime.fromisoformat(info["checked_at"])).total_seconds()
    return age >= max(60.0, every_minutes * 60 - 300)


def check(config: Path, store, *, summary: bool = False, force: bool = False, dry_run: bool = False,
          budget: float = RUN_BUDGET_SECONDS, dashboard_out: Path | None = None) -> dict:
    """Check the watches that are due, update the store, send alerts.

    Returns a small report: searches checked, alerts sent, exit code.
    """
    global _deadline
    _deadline = time.monotonic() + budget
    settings, watches = load_config(Path(config))
    state = store.load_state()
    now_dt = datetime.now(IST).replace(microsecond=0)
    now = now_dt.isoformat()
    report = {"checked": 0, "answered": 0, "alerts": 0, "exit_code": 0, "skipped_busy": False}

    active = [w for w in watches if w.last_date >= now_dt.date().isoformat()]
    for w in watches:
        if w not in active:
            log(f"Skipping (departure has passed): {w.name}")

    needed: dict[tuple, set[tuple]] = {}
    for w in active:
        needed.setdefault(w.search_key, set())
        if w.flights:
            needed[w.search_key].add(w.flights)
    searches = state.setdefault("searches", {})
    for sid in [k for k in searches if k not in {search_id(k2) for k2 in needed}]:
        del searches[sid]  # search no longer used by any watch
    due = [k for k in needed if _due(k, searches, now_dt, settings["check_every_minutes"], force)]
    due.sort(key=lambda k: (searches.get(search_id(k)) or {}).get("checked_at", ""))  # oldest first

    results: dict[tuple, list[Option] | None] = {}
    incomplete: set[tuple] = set()
    for i, key in enumerate(due):
        if out_of_time():
            log("Run time budget used up; the remaining searches wait for the next run.")
            break
        origin, destination, depart, ret = key
        log(f"Searching direct {origin}->{destination} {depart}" + (f" return {ret}" if ret else ""))
        if i:
            time.sleep(PAUSE_BETWEEN_SEARCHES)
        results[key] = fetch_options(key, settings, needed[key])
        found = {o.flights for o in results[key] or []}
        if results[key] is not None and out_of_time() and not needed[key] <= found:
            # Cut short by the time budget: watched flights may simply not have
            # been reached, so do not count them as missing; retry next run.
            incomplete.add(key)
        searches[search_id(key)] = {"checked_at": now, "ok": results[key] is not None and key not in incomplete}
        n = len(results[key] or [])
        log(f"  {n} direct option(s)" if n else "  no answer from Google after retries")
    report["checked"] = len(results)
    report["answered"] = sum(1 for v in results.values() if v)

    messages, history_rows, event_rows = [], [], []
    wstate = state.setdefault("watches", {})
    if results and not any(results.values()):
        state["failed_runs"] = state.get("failed_runs", 0) + 1
        log(f"No prices at all this check ({state['failed_runs']} in a row).")
        if state["failed_runs"] == BROKEN_RUNS_ALERT:
            messages.append(
                f"🛑 Flight tracker: no prices from Google Flights for {BROKEN_RUNS_ALERT} checks in a row. "
                "Google may have changed something; the 'flights' library may need an update "
                "(edit requirements.txt). Your Google Flights alerts still work meanwhile.")
        if state["failed_runs"] >= BROKEN_RUNS_ALERT:
            report["exit_code"] = 2
    elif results:
        state["failed_runs"] = 0

    # Start-up: the first prices can take a few runs to arrive (each run checks
    # what fits in its time budget). New watches stay quiet for the first two
    # hours, and one overview is sent once every watch has a price.
    started = datetime.fromisoformat(state.setdefault("started_at", now))
    starting_up = (now_dt - started) < timedelta(hours=2)
    if results:
        log("")
    for w in active:
        if w.search_key not in results:
            continue  # not due this run
        options = results[w.search_key]
        if options is None:
            continue  # Google did not answer: not counted as "flight missing"
        option = pick(w, options)
        if option is None and w.search_key in incomplete:
            continue  # not reached before the time budget ran out
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
            event_rows.append({
                "checked_at": now, "watch_id": w.id, "kind": kind,
                "price": f"{st['last']:.0f}" if kind != "missing" else "",
                "reference": f"{info['ref']:.0f}" if "ref" in info else ""})
            if kind == "baseline":
                if not starting_up:
                    messages.append(f"👀 Now watching: {w.name}\n{inr(st['last'])} · "
                                    f"{describe_flights(option.flights, option.times)}\n{google_link(w, option)}")
                continue
            messages.append(event_message(kind, info, w, st, option))

    known = {w.id for w in watches}
    for wid in [k for k in wstate if k not in known]:
        del wstate[wid]  # watch removed from watches.toml

    first_run = not state.get("overview_sent") and bool(active) and \
        (all("last" in wstate.get(w.id, {}) for w in active) or not starting_up)
    if first_run:
        state["overview_sent"] = True
    if first_run or summary:
        stamp = now_dt.strftime("%-d %b %Y, %H:%M IST")
        title = "✈️ Flight tracker started. Current direct prices:" if first_run and not summary \
            else f"✈️ Daily flight price summary ({stamp})"
        history = store.load_history()
        messages.append(summary_message(active, wstate, history, now_dt, title))

    dashboard_path = None
    if dry_run:
        log("\n(dry run: nothing saved)")
    else:
        if results:
            state["updated_at"] = now
        store.save_state(state)
        store.add_history(history_rows)
        store.add_events(event_rows)
        if any(v is not None for v in results.values()):
            save_latest(store, results, now)
        if dashboard_out:
            dashboard_path = build_dashboard_safely(Path(config), store, dashboard_out)

    attach = dashboard_path if (first_run or summary) and settings["send_dashboard"] else None
    if messages and not send("\n\n".join(messages), dry_run, attach):
        report["exit_code"] = report["exit_code"] or 1
    report["alerts"] = len(messages)
    if not messages:
        log("\nNo alerts this time.")
    return report


def run(args) -> int:
    if args.test_notify:
        load_config(Path(args.config))
        ok = send("✈️ Test message from your flight tracker. Price alerts will arrive here.", False)
        return 0 if ok else 1
    store = open_store(Path(args.data_dir))
    report = check(Path(args.config), store, summary=args.summary, force=args.force, dry_run=args.dry_run,
                   dashboard_out=None if store.kind == "redis" else Path(args.dashboard))
    return report["exit_code"]


def save_latest(store, results: dict, now: str) -> None:
    """Every direct option per search at its latest successful check."""
    searches = store.load_latest().get("searches", {})
    for key, options in results.items():
        if options is None:
            continue  # keep the previous snapshot for searches Google did not answer
        searches[search_id(key)] = {
            "checked_at": now,
            "options": [{"price": o.price, "flights": list(o.flights), "legs": list(o.legs)} for o in options],
        }
    store.save_latest({"updated_at": now, "searches": searches})


def build_dashboard_safely(config: Path, store, out: Path) -> Path | None:
    try:
        import dashboard
        dashboard.build(config, store, out)
        log(f"Dashboard written to {out}")
        return out
    except Exception as e:  # the dashboard must never stop alerts
        log(f"Dashboard not built: {type(e).__name__}: {e}")
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(HERE / "watches.toml"), help="watch list (default: watches.toml)")
    parser.add_argument("--data-dir", default=str(HERE / "data"), help="where prices are saved (default: data/)")
    parser.add_argument("--dashboard", default=str(HERE / "dashboard" / "index.html"),
                        help="where to write the dashboard page (default: dashboard/index.html)")
    parser.add_argument("--summary", action="store_true", help="also send a summary of every watch")
    parser.add_argument("--force", action="store_true", help="check every watch, even if checked recently")
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
