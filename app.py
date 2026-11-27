"""The web app (runs on Vercel through api/index.py; also locally for testing).

Pages (all except /health and /api/cron need the shared password):
  /                 the price dashboard
  /login, /logout   one shared password (DASHBOARD_PASSWORD), remembered 90 days
  /check-now        POST: check every watch now
  /setup            what is configured, test message, create the schedule
  /api/cron         scheduled check (QStash every 10 min, or cron-job.org)
  /api/cron/summary daily summary (Vercel's own daily cron)
  /health           last check time, no prices

Environment variables:
  DASHBOARD_PASSWORD  the shared password
  CRON_SECRET         long random text; schedulers send "Authorization: Bearer <it>"
  UPSTASH_REDIS_REST_URL / _TOKEN (or KV_REST_API_URL / _TOKEN)  storage
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  alerts
  QSTASH_TOKEN (+ QSTASH_URL)  only for creating the schedule from /setup
  APP_URL             optional; defaults to the Vercel production address
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import dashboard
import tracker
from store import FileStore, RedisStore, redis_env

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "watches.toml"
COOKIE = "ft_session"
SESSION_SECONDS = 90 * 24 * 3600
CHECK_BUDGET_SECONDS = 45  # the Vercel function stops at 60 s
LOCK_SECONDS = 70
QSTASH_SCHEDULE_ID = "flight-tracker-check"
STATIC = HERE / "static"
# Installable-app files: no prices or routes in them, so no password needed.
PUBLIC_FILES = {
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json", "public, max-age=3600"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8", "no-cache"),
    "/icon-192.png": ("icon-192.png", "image/png", "public, max-age=604800"),
    "/icon-512.png": ("icon-512.png", "image/png", "public, max-age=604800"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png", "public, max-age=604800"),
    "/favicon.ico": ("favicon-32.png", "image/png", "public, max-age=604800"),
    "/favicon-32.png": ("favicon-32.png", "image/png", "public, max-age=604800"),
}
PWA_HEAD = """<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Flights">
<meta name="apple-mobile-web-app-status-bar-style" content="default">"""
QSTASH_CRON = "*/10 * * * *"

Response = tuple[int, list[tuple[str, str]], bytes]


# --------------------------------------------------------------------------
# Plumbing
# --------------------------------------------------------------------------

def get_store():
    env = redis_env()
    if env:
        return RedisStore(*env)
    if os.environ.get("VERCEL"):
        return None  # Vercel's disk is not kept: storage must be connected
    return FileStore(Path(os.environ.get("FT_DATA_DIR", HERE / "data")))


def base_url(headers: dict) -> str:
    url = tracker.app_url()
    if url:
        return url
    proto = headers.get("x-forwarded-proto", "http")
    return f"{proto}://{headers.get('host', 'localhost')}"


def _key() -> bytes:
    return hashlib.sha256(("flight-tracker-session:" + os.environ.get("DASHBOARD_PASSWORD", "")).encode()).digest()


def make_session(now: float | None = None) -> str:
    exp = str(int((now or time.time()) + SESSION_SECONDS))
    return exp + "." + hmac.new(_key(), exp.encode(), hashlib.sha256).hexdigest()


def valid_session(value: str | None, now: float | None = None) -> bool:
    if not value or "." not in value or not os.environ.get("DASHBOARD_PASSWORD"):
        return False
    exp, sig = value.split(".", 1)
    good = hmac.new(_key(), exp.encode(), hashlib.sha256).hexdigest()
    return exp.isdigit() and int(exp) > (now or time.time()) and hmac.compare_digest(sig, good)


def cookies(headers: dict) -> dict:
    out = {}
    for part in headers.get("cookie", "").split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k] = v
    return out


def authed(headers: dict) -> bool:
    return valid_session(cookies(headers).get(COOKIE))


def cron_authed(headers: dict) -> bool:
    secret = os.environ.get("CRON_SECRET", "")
    got = headers.get("authorization", "")
    return bool(secret) and hmac.compare_digest(got, f"Bearer {secret}")


SECURITY_HEADERS = [("Cache-Control", "no-store"), ("X-Robots-Tag", "noindex, nofollow"),
                    ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"),
                    ("X-Content-Type-Options", "nosniff")]


def page(status: int, body: str, extra: list | None = None) -> Response:
    return status, [("Content-Type", "text/html; charset=utf-8")] + SECURITY_HEADERS + (extra or []), body.encode()


def as_json(status: int, obj) -> Response:
    return status, [("Content-Type", "application/json")] + SECURITY_HEADERS, json.dumps(obj).encode()


def redirect(to: str, extra: list | None = None) -> Response:
    return 303, [("Location", to)] + SECURITY_HEADERS + (extra or []), b""


def simple_page(title: str, inner: str) -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>{html.escape(title)}</title>{PWA_HEAD}<meta name="theme-color" content="#e7e5e0"><style>
:root{{color-scheme:light dark;--bg:#f9f9f7;--card:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--border:rgba(11,11,11,.12);--accent:#2a78d6;--good:#006300;--bad:#b42525}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0d0d0d;--card:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--border:rgba(255,255,255,.12);--accent:#3987e5;--good:#0ca30c;--bad:#e66767}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
.wrap{{max-width:640px;margin:0 auto;padding:40px 16px}}
.card{{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:20px;margin-top:16px}}
h1{{font-size:24px;margin:0}} p{{color:var(--ink2)}} a{{color:var(--accent)}}
input,button{{font:inherit;border-radius:10px;padding:10px 12px;border:1px solid var(--border);background:var(--card);color:var(--ink)}}
input{{width:100%;box-sizing:border-box;margin:8px 0 12px}}
button{{background:var(--accent);color:#fff;border:0;font-weight:600;cursor:pointer}}
.ok{{color:var(--good);font-weight:600}} .no{{color:var(--bad);font-weight:600}}
li{{margin:6px 0}} code{{font-size:13px;word-break:break-all}}
</style></head><body><div class="wrap">{inner}</div></body></html>"""


def login_page(error: str = "") -> str:
    err = f'<p class="no">{html.escape(error)}</p>' if error else ""
    return simple_page("Flight prices", f"""<h1>Flight prices</h1>
<div class="card"><form method="post" action="/login">{err}
<label for="pw">Password</label>
<input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>
<button type="submit">Open dashboard</button></form></div>
<script>
// Logged out or not yet logged in: forget the offline copy of the dashboard.
try {{ if ("caches" in window) caches.delete("flight-prices-v1"); }} catch (e) {{}}
</script>""")


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------

def run_check(store, *, summary: bool = False, force: bool = False) -> dict:
    """One check, never two at once (a scheduled run and a button press)."""
    if not store.acquire_lock(LOCK_SECONDS):
        return {"skipped_busy": True, "checked": 0, "answered": 0, "alerts": 0, "exit_code": 0}
    try:
        return tracker.check(CONFIG, store, summary=summary, force=force, budget=CHECK_BUDGET_SECONDS)
    finally:
        store.release_lock()


def qstash(method: str, path: str, headers: dict | None = None, opener=urllib.request.urlopen):
    base = os.environ.get("QSTASH_URL", "https://qstash.upstash.io").rstrip("/")
    req = urllib.request.Request(base + path, method=method, data=b"" if method == "POST" else None,
                                 headers={"Authorization": f"Bearer {os.environ['QSTASH_TOKEN']}", **(headers or {})})
    with opener(req, timeout=20) as resp:
        raw = resp.read().decode()
        return json.loads(raw) if raw else None


def create_schedule(target_base: str, opener=urllib.request.urlopen) -> dict:
    """Create (or update) the every-10-minutes QStash schedule that calls /api/cron."""
    return qstash("POST", "/v2/schedules/" + target_base + "/api/cron", {
        "Upstash-Cron": QSTASH_CRON,
        "Upstash-Schedule-Id": QSTASH_SCHEDULE_ID,
        "Upstash-Method": "POST",
        "Upstash-Retries": "0",
        "Upstash-Forward-Authorization": f"Bearer {os.environ['CRON_SECRET']}",
    }, opener)


def setup_page(headers: dict, store, notice: str = "") -> str:
    def row(ok: bool, text: str) -> str:
        return f'<li><span class="{"ok" if ok else "no"}">{"✓" if ok else "✗"}</span> {text}</li>'
    items = [
        row(bool(os.environ.get("DASHBOARD_PASSWORD")), "Password set (DASHBOARD_PASSWORD)"),
        row(bool(os.environ.get("CRON_SECRET")), "Scheduler secret set (CRON_SECRET)"),
    ]
    store_ok, store_text = False, "Storage: connect Upstash Redis in Vercel (Storage tab)"
    if store is not None:
        try:
            store.load_state()
            store_ok, store_text = True, f"Storage reachable ({store.kind})"
        except Exception as e:
            store_text = f"Storage not reachable: {html.escape(type(e).__name__)}"
    items.append(row(store_ok, store_text))
    tg = bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))
    items.append(row(tg, "Telegram alerts (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)"))
    sched = "QStash not connected: add it in Vercel (Marketplace → Upstash → QStash), or use cron-job.org"
    has_q = bool(os.environ.get("QSTASH_TOKEN"))
    sched_ok = False
    if has_q:
        try:
            listed = qstash("GET", "/v2/schedules") or []
            mine = [s for s in listed if s.get("scheduleId") == QSTASH_SCHEDULE_ID]
            sched_ok = bool(mine)
            sched = (f"QStash schedule active ({html.escape(mine[0].get('cron', ''))} → "
                     f"<code>{html.escape(mine[0].get('destination', ''))}</code>)") if mine else \
                "QStash connected; schedule not created yet (button below)"
        except Exception as e:
            sched = f"QStash connected but not reachable: {html.escape(type(e).__name__)}"
    items.append(row(sched_ok, sched))
    state = store.load_state() if store_ok else {}
    last = state.get("updated_at", "never")
    base = html.escape(base_url(headers))
    note = f'<p class="ok">{html.escape(notice)}</p>' if notice else ""
    buttons = ""
    if has_q:
        buttons += '<form method="post" action="/setup"><input type="hidden" name="do" value="schedule">' \
                   '<button>Create / update the 10-minute schedule</button></form><br>'
    if tg:
        buttons += '<form method="post" action="/setup"><input type="hidden" name="do" value="telegram">' \
                   '<button>Send a test Telegram message</button></form>'
    return simple_page("Setup", f"""<h1>Setup</h1>{note}
<div class="card"><ul style="list-style:none;padding:0">{''.join(items)}</ul>
<p>Last check: {html.escape(str(last))}<br>App address: <code>{base}</code><br>
Scheduler URL: <code>{base}/api/cron</code> with header <code>Authorization: Bearer &lt;CRON_SECRET&gt;</code></p>
{buttons}</div><p><a href="/">← Dashboard</a></p>""")


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------

def handle(method: str, raw_path: str, headers: dict, body: bytes) -> Response:
    headers = {k.lower(): v for k, v in headers.items()}
    url = urllib.parse.urlsplit(raw_path)
    query = urllib.parse.parse_qs(url.query)
    path = query.pop("path", [url.path])[0] or "/"  # Vercel rewrites pass the original path here
    path = "/" + path.lstrip("/")
    form = urllib.parse.parse_qs(body.decode("utf-8", "replace")) if method == "POST" else {}

    if path in PUBLIC_FILES and method in ("GET", "HEAD"):
        name, ctype, cache = PUBLIC_FILES[path]
        try:
            body = (STATIC / name).read_bytes()
        except OSError:
            return page(404, simple_page("Not found", "<h1>Not found</h1>"))
        extra = [("Service-Worker-Allowed", "/")] if name == "sw.js" else []
        return 200, [("Content-Type", ctype), ("Cache-Control", cache), ("X-Content-Type-Options", "nosniff")] + extra, body

    if path == "/health":  # no prices or routes here: safe without a password
        store = get_store()
        try:
            watches = len(tracker.load_config(CONFIG)[1])
            template_ok = dashboard.TEMPLATE.exists()
            state = store.load_state() if store else {}
            return as_json(200, {"ok": store is not None and template_ok, "storage": store.kind if store else None,
                                 "watches": watches, "page_template": template_ok,
                                 "updated_at": state.get("updated_at"), "failed_runs": state.get("failed_runs", 0),
                                 "google": {k: (state.get("google") or {}).get(k) for k in
                                            ("last_answer_at", "answered_by", "refused_runs", "pause_until", "last_problem",
                                                      "checkers")}})
        except Exception as e:
            return as_json(500, {"ok": False, "error": type(e).__name__})

    if path in ("/api/cron", "/api/cron/summary"):
        if not cron_authed(headers):
            return as_json(401, {"error": "unauthorized"})
        store = get_store()
        if store is None:
            return as_json(500, {"error": "storage not connected"})
        report = run_check(store, summary=path.endswith("/summary"))
        return as_json(200, report)

    if not os.environ.get("DASHBOARD_PASSWORD"):
        return page(500, simple_page("Setup needed", "<h1>Almost there</h1><div class='card'><p>Set the "
                                     "<code>DASHBOARD_PASSWORD</code> environment variable in Vercel, then redeploy.</p></div>"))

    if path == "/login":
        if method == "POST":
            given = (form.get("password") or [""])[0]
            if hmac.compare_digest(given.encode(), os.environ["DASHBOARD_PASSWORD"].encode()):
                cookie = (f"{COOKIE}={make_session()}; Path=/; Max-Age={SESSION_SECONDS}; HttpOnly; "
                          f"SameSite=Lax" + ("; Secure" if base_url(headers).startswith("https") else ""))
                return redirect("/", [("Set-Cookie", cookie)])
            time.sleep(1)  # slow down guessing
            return page(401, login_page("Wrong password"))
        return page(200, login_page())

    if path == "/logout":
        return redirect("/login", [("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")])

    if not authed(headers):
        return redirect("/login") if path != "/" else page(200, login_page())

    store = get_store()
    if path == "/setup":
        notice = ""
        if method == "POST":
            action = (form.get("do") or [""])[0]
            try:
                if action == "schedule":
                    create_schedule(base_url(headers))
                    notice = "Schedule saved: prices are now checked every 10 minutes."
                elif action == "telegram":
                    tracker.send("✈️ Test message from your flight tracker. Price alerts will arrive here.", False)
                    notice = "Test message sent: check Telegram."
            except Exception as e:
                notice = f"That did not work: {type(e).__name__}: {e}"
        return page(200, setup_page(headers, store, notice))

    if store is None:
        return redirect("/setup")

    if path == "/check-now" and method == "POST":
        report = run_check(store, force=True)
        if report.get("skipped_busy"):
            msg = "busy"
        else:
            msg = f"{report['answered']}-{report['checked']}"
        return redirect("/?checked=" + msg)

    if path == "/":
        data = dashboard.collect(CONFIG, store)
        notice = None
        checked = (query.get("checked") or [""])[0]
        if checked == "busy":
            notice = "A check is already running; refresh in a minute."
        elif checked:
            answered, _, total = checked.partition("-")
            notice = f"Checked {total} route(s) now; Google answered {answered}."
        data["web"] = {"check_now": "/check-now", "logout": "/logout", "setup": "/setup", "notice": notice}
        return page(200, dashboard.render(data, extra_head=PWA_HEAD))

    return page(404, simple_page("Not found", "<h1>Not found</h1><p><a href='/'>Dashboard</a></p>"))
