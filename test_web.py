"""Offline tests for storage, the check loop and the web app. Run: python -m unittest -v"""
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import app
import store as store_mod
import tracker

CONFIG = Path(__file__).with_name("watches.toml")


class FakeUpstash:
    """Just enough of Upstash's REST API for RedisStore."""

    def __init__(self):
        self.kv, self.lists, self.hashes = {}, {}, {}
        self.calls = 0

    def run(self, c):
        op, *a = c
        op = op.upper()
        if op == "GET":
            return self.kv.get(a[0])
        if op == "SET":
            if "NX" in a and a[0] in self.kv:
                return None
            self.kv[a[0]] = a[1]
            return "OK"
        if op == "DEL":
            return int(self.kv.pop(a[0], None) is not None)
        if op == "RPUSH":
            self.lists.setdefault(a[0], []).extend(a[1:])
            return len(self.lists[a[0]])
        if op == "LRANGE":
            lst = self.lists.get(a[0], [])
            start, stop = int(a[1]), int(a[2])
            return lst[start:] if stop == -1 else lst[start:stop + 1]
        if op == "LTRIM":
            lst = self.lists.get(a[0], [])
            self.lists[a[0]] = lst[int(a[1]):] if int(a[2]) == -1 else lst[int(a[1]):int(a[2]) + 1]
            return "OK"
        if op == "HGETALL":
            return [x for kv in self.hashes.get(a[0], {}).items() for x in kv]
        if op == "HSET":
            self.hashes.setdefault(a[0], {})[a[1]] = a[2]
            return 1
        if op == "KEYS":
            prefix = a[0].rstrip("*")
            return [k for k in self.lists if k.startswith(prefix)]
        raise AssertionError(f"unexpected command {c}")

    def __call__(self, req, timeout=None):
        self.calls += 1
        body = json.loads(req.data.decode())
        out = [{"result": self.run(c)} for c in body] if req.full_url.endswith("/pipeline") \
            else {"result": self.run(body)}
        return io.BytesIO(json.dumps(out).encode())


def redis():
    return store_mod.RedisStore("https://fake.upstash.io", "t", opener=FakeUpstash())


class Stores(unittest.TestCase):
    def roundtrip(self, s):
        s.save_state({"watches": {"a": {"last": 1}}})
        self.assertEqual(s.load_state()["watches"]["a"]["last"], 1)
        s.save_latest({"searches": {"x": {}}})
        self.assertIn("x", s.load_latest()["searches"])
        s.add_events([{"checked_at": "t1", "watch_id": "a", "kind": "drop", "price": "9", "reference": "10"}])
        self.assertEqual(s.load_events()[0]["kind"], "drop")

    def test_file_store(self):
        with tempfile.TemporaryDirectory() as d:
            self.roundtrip(store_mod.FileStore(Path(d)))

    def test_redis_store(self):
        self.roundtrip(redis())

    def test_redis_history_keeps_only_changes(self):
        s = redis()
        row = lambda t, p: {"checked_at": t, "watch_id": "a", "price": p, "flights": "6E 1", "status": "ok"}
        s.add_history([row("t1", "100")])
        s.add_history([row("t2", "100")])
        s.add_history([row("t3", "90")])
        self.assertEqual([r["checked_at"] for r in s.load_history()], ["t1", "t3"])

    def test_redis_lock_is_exclusive(self):
        s = redis()
        other = store_mod.RedisStore(s.url, s.token, opener=s._open)
        self.assertTrue(s.acquire_lock(60))
        self.assertFalse(other.acquire_lock(60))
        s.release_lock()
        self.assertTrue(other.acquire_lock(60))

    def test_open_store_prefers_redis_env(self):
        with mock.patch.dict(os.environ, {"KV_REST_API_URL": "https://x", "KV_REST_API_TOKEN": "y"}, clear=False):
            self.assertEqual(store_mod.open_store(Path(".")).kind, "redis")


def fake_options(key, settings, needed, **_):
    origin, dest, depart, ret = key
    if ".." in depart:
        return [tracker.Option(20000, ("2027-01-17",), ("Sun 17 Jan",)),
                tracker.Option(21000, ("2027-01-18",), ("Mon 18 Jan",))]
    concrete = [tuple("ZZ 9" if x == tracker.ANY else x.split("|")[0] for x in f) for f in sorted(needed)]
    opts = [tracker.Option(30000 + i, f, ("t",) * len(f)) for i, f in enumerate(concrete)]
    cheap = ("ZZ 1", "ZZ 2") if ret else ("ZZ 1",)
    return opts + [tracker.Option(10000, cheap, ("t",) * len(cheap))]


def fake_grid(origin, destination, start, end, days, settings):
    """Made-up date grid: Fridays cost 30000, other days 25000, plus 100 per day of trip length."""
    out, d = {}, start
    while d <= end:
        r = d + timedelta(days=days) if days else None
        key = (d.isoformat(),) + ((r.isoformat(),) if r else ())
        out[key] = (30000 if d.weekday() == 4 else 25000) + 100 * (days or 0)
        d += timedelta(days=1)
    return out


class CheckLoop(unittest.TestCase):
    def setUp(self):
        self.store = redis()
        g = mock.patch.object(tracker, "_date_grid", side_effect=fake_grid)
        g.start()
        self.addCleanup(g.stop)
        a = mock.patch.dict(tracker.DEFAULT_SETTINGS, {"alerts": ["drop", "rise", "target", "started", "broken"]})
        a.start()
        self.addCleanup(a.stop)
        p = mock.patch.object(tracker, "fetch_options", side_effect=fake_options)
        p.start()
        self.addCleanup(p.stop)
        z = mock.patch.object(tracker.time, "sleep")
        z.start()
        self.addCleanup(z.stop)
        q = mock.patch.object(tracker, "send", return_value=True)
        self.send = q.start()
        self.addCleanup(q.stop)

    def test_first_check_then_nothing_due(self):
        r1 = tracker.check(CONFIG, self.store)
        _, watches = tracker.load_config(CONFIG)
        self.assertEqual(r1["checked"], len({w.search_key for w in watches}))
        self.assertIn("Flight tracker started", self.send.call_args[0][0])
        r2 = tracker.check(CONFIG, self.store)
        self.assertEqual(r2["checked"], 0)  # everything checked moments ago
        r3 = tracker.check(CONFIG, self.store, force=True)
        self.assertEqual(r3["checked"], r1["checked"])

    def test_one_overview_after_a_partial_first_run(self):
        google_down_for_delhi = {"on": True}

        def flaky(key, settings, needed, **kw):
            if key[0] == "DEL" and google_down_for_delhi["on"]:
                return None
            return fake_options(key, settings, needed)
        with mock.patch.object(tracker, "fetch_options", side_effect=flaky):
            tracker.check(CONFIG, self.store)  # Delhi routes get no answer
            texts = [c[0][0] for c in self.send.call_args_list]
            self.assertFalse(any("started" in t for t in texts))
            google_down_for_delhi["on"] = False
            tracker.check(CONFIG, self.store)  # retried (failed searches are due), now answered
        texts = [c[0][0] for c in self.send.call_args_list]
        self.assertEqual(sum("Flight tracker started" in t for t in texts), 1)
        self.assertFalse(any("Now watching" in t for t in texts))

    def test_one_failing_route_is_not_google_down(self):
        tracker.check(CONFIG, self.store)  # every route answers
        state = self.store.load_state()
        sid = "DEL|SIN|2027-03-02|2027-03-07"
        state["searches"][sid]["ok"] = False  # only this route is due, and keeps failing
        self.store.save_state(state)
        with mock.patch.object(tracker, "fetch_options", return_value=None):
            for _ in range(4):
                tracker.check(CONFIG, self.store)
        self.assertEqual(self.store.load_state()["failed_runs"], 0)
        self.assertFalse(any("no prices from Google" in c[0][0] for c in self.send.call_args_list))

    def test_refusal_stops_the_run_and_backs_off(self):
        calls = []

        def refused(key, settings, needed, **kw):
            calls.append(key)
            return None
        with mock.patch.object(tracker, "fetch_options", side_effect=refused), \
                mock.patch.object(tracker, "refresh_insights") as ins:
            r1 = tracker.check(CONFIG, self.store)
            self.assertEqual(len(calls), 1)  # stopped at the first refusal
            ins.assert_not_called()  # no date comparisons while Google refuses
            g = self.store.load_state()["google"]
            self.assertEqual(g["refused_runs"], 1)
            self.assertIn("pause_until", g)
            r2 = tracker.check(CONFIG, self.store)  # inside the 20-minute pause: nothing searched
            self.assertTrue(r2["paused"])
            self.assertEqual(len(calls), 1)
            tracker.check(CONFIG, self.store, force=True)  # the Check now button ignores the pause
            self.assertEqual(len(calls), 2)
            self.assertEqual(self.store.load_state()["google"]["refused_runs"], 2)
        self.assertFalse(r1["paused"])
        tracker.check(CONFIG, self.store, force=True)  # Google answers again
        g = self.store.load_state()["google"]
        self.assertEqual(g["refused_runs"], 0)
        self.assertNotIn("pause_until", g)
        self.assertTrue(g["last_answer_at"])

    def test_budget_cut_does_not_count_as_missing(self):
        budget = {"used_up": False}

        def partial(key, settings, needed, **kw):
            budget["used_up"] = True  # the budget runs out during this search
            return [tracker.Option(10000, ("ZZ 1", "ZZ 2") if key[3] else ("ZZ 1",), ("t", "t"))]
        with mock.patch.object(tracker, "fetch_options", side_effect=partial), \
                mock.patch.object(tracker, "out_of_time", side_effect=lambda: budget["used_up"]):
            tracker.check(CONFIG, self.store)
        state = self.store.load_state()
        exact = "bengaluru-singapore-indigo-6e-1457-6e-1458"
        self.assertNotIn(exact, state["watches"])  # not marked as a miss
        self.assertFalse(state["searches"]["BLR|SIN|2027-03-02|2027-03-07"]["ok"])  # retried next run

    def test_default_alerts_are_price_changes_only(self):
        with mock.patch.dict(tracker.DEFAULT_SETTINGS, {"alerts": ["drop", "rise", "target"]}):
            tracker.check(CONFIG, self.store)  # first prices: no "started" message (a target hit is allowed)
            self.assertFalse(any("Flight tracker started" in c[0][0] for c in self.send.call_args_list))
            state = self.store.load_state()
            for info in state["searches"].values():
                info["checked_at"] = "2000-01-01T00:00:00+05:30"  # make everything due again
            self.store.save_state(state)
            cheaper = lambda key, settings, needed, **kw: [  # noqa: E731
                tracker.Option(o.price - 2000, o.flights, o.times) for o in fake_options(key, settings, needed)]
            with mock.patch.object(tracker, "fetch_options", side_effect=cheaper):
                tracker.check(CONFIG, self.store)
        sent = self.send.call_args[0][0]
        self.assertIn("PRICE DROP", sent)
        self.assertNotIn("started", sent)

    def test_insights_saved_for_dashboard(self):
        tracker.check(CONFIG, self.store)
        ins = self.store.load_latest()["insights"]["BLR|HKT|2027-01-22|2027-01-26"]
        self.assertTrue(ins["ok"])
        self.assertEqual(ins["days"], 4)
        self.assertTrue(all(datetime.fromisoformat(x["depart"]).weekday() == 4 for x in ins["similar"]))  # Fridays
        self.assertEqual(len(ins["nearby"]), 9)  # Thu/Fri/Sat x Mon/Tue/Wed
        self.assertEqual(ins["yours"], 30400)

    def test_insights_drop_nonsense_prices(self):
        def glitchy(origin, destination, start, end, days, settings):
            out = fake_grid(origin, destination, start, end, days, settings)
            if days == 2:  # e.g. Sat -> Mon
                out = {k: 413 for k in out}
            return out
        _, watches = tracker.load_config(CONFIG)
        w = next(x for x in watches if x.destination == "HKT")
        r = tracker.trip_insights(w, tracker.DEFAULT_SETTINGS, datetime(2026, 11, 27).date(), grid_fn=glitchy)
        self.assertNotIn(413, [x["price"] for x in r["nearby"]])
        self.assertEqual(sum(1 for x in r["nearby"] if x["price"] is None), 1)

    def test_flexible_watch_takes_cheapest_date(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write('[[watch]]\nname="flex"\nfrom="BLR"\nto="HKT"\ndepart="2027-01-15"\ndepart_until="2027-01-31"\n')
        tracker.check(Path(fh.name), self.store)
        st = self.store.load_state()["watches"]["flex"]
        self.assertEqual((st["last"], st["last_flights"]), (20000, ["2027-01-17"]))

    def test_any_return_takes_cheapest_matching_pair(self):
        _, watches = tracker.load_config(CONFIG)
        w = next(x for x in watches if x.flights and tracker.ANY in x.flights)
        opts = [tracker.Option(36661, ("QP 623", "IX 931"), ()), tracker.Option(39089, ("6E 1217", "IX 931"), ()),
                tracker.Option(42850, ("6E 1217", "6E 1218"), ()), tracker.Option(30000, ("AI 1", "IX 931"), ())]
        self.assertEqual(tracker.pick(w, opts).flights, ("QP 623", "IX 931"))  # either outbound; Air India is not
        self.assertEqual(len(tracker.matching_options(w, opts)), 3)
        self.assertTrue(tracker.all_found({w.flights}, [o.flights for o in opts]))
        self.assertFalse(tracker.all_found({w.flights}, [("AI 1", "IX 931")]))
        self.assertEqual(tracker.normalize_flight("6e1217 | qp 623"), "6E 1217|QP 623")

    def test_due_after_interval(self):
        now = datetime(2027, 1, 1, 12, 0, tzinfo=tracker.IST)
        searches = {"A|B|2027-01-02|": {"checked_at": (now - timedelta(minutes=20)).isoformat(), "ok": True}}
        key = ("A", "B", "2027-01-02", None)
        self.assertFalse(tracker._due(key, searches, now, 30, False))
        searches["A|B|2027-01-02|"]["checked_at"] = (now - timedelta(minutes=26)).isoformat()
        self.assertTrue(tracker._due(key, searches, now, 30, False))
        searches["A|B|2027-01-02|"]["ok"] = False
        searches["A|B|2027-01-02|"]["checked_at"] = now.isoformat()
        self.assertTrue(tracker._due(key, searches, now, 30, False))  # failed last time: retry


class FlexibleConfig(unittest.TestCase):
    def load(self, body):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write(body)
        return tracker.load_config(Path(fh.name))

    def test_window_and_trip_days(self):
        _, (w,) = self.load('[[watch]]\nname="x"\nfrom="BLR"\nto="SIN"\ndepart="2027-03-01"\n'
                            'depart_until="2027-03-10"\ntrip_days=5\n')
        self.assertTrue(w.flexible)
        self.assertEqual(w.search_id, "BLR|SIN|2027-03-01..2027-03-10|5d")

    def test_rejects_return_with_window(self):
        with self.assertRaises(tracker.ConfigError):
            self.load('[[watch]]\nname="x"\nfrom="BLR"\nto="SIN"\ndepart="2027-03-01"\n'
                      'depart_until="2027-03-10"\nreturn="2027-03-12"\n')

    def test_trip_days_needs_window(self):
        with self.assertRaises(tracker.ConfigError):
            self.load('[[watch]]\nname="x"\nfrom="BLR"\nto="SIN"\ndepart="2027-03-01"\ntrip_days=5\n')


ENV = {"DASHBOARD_PASSWORD": "open sesame", "CRON_SECRET": "s3cret"}


class WebApp(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, ENV)
        p.start()
        self.addCleanup(p.stop)
        for k in ("UPSTASH_REDIS_REST_URL", "KV_REST_API_URL", "VERCEL", "APP_URL", "VERCEL_PROJECT_PRODUCTION_URL"):
            os.environ.pop(k, None)
        self.store = redis()
        g = mock.patch.object(app, "get_store", return_value=self.store)
        g.start()
        self.addCleanup(g.stop)

    def call(self, method, path, headers=None, body=b""):
        return app.handle(method, path, {"host": "example.test", **(headers or {})}, body)

    def login(self):
        status, headers, _ = self.call("POST", "/login", body=b"password=open+sesame")
        self.assertEqual(status, 303)
        cookie = dict(headers)["Set-Cookie"].split(";")[0]
        return {"cookie": cookie}

    def test_dashboard_needs_password(self):
        status, _, body = self.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'name="password"', body)
        self.assertNotIn(b"Bengaluru", body)

    def test_wrong_password(self):
        with mock.patch.object(app.time, "sleep"):
            status, _, body = self.call("POST", "/login", body=b"password=nope")
        self.assertEqual(status, 401)

    def test_login_then_dashboard(self):
        status, _, body = self.call("GET", "/", self.login())
        self.assertEqual(status, 200)
        self.assertIn(b"Bengaluru", body)
        self.assertIn(b'"check_now"', body)

    def test_vercel_rewrite_path(self):
        status, _, body = self.call("GET", "/api/index?path=/health")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_session_expires_and_changes_with_password(self):
        token = app.make_session(now=1000)
        self.assertTrue(app.valid_session(token, now=1001))
        self.assertFalse(app.valid_session(token, now=1000 + app.SESSION_SECONDS + 1))
        with mock.patch.dict(os.environ, {"DASHBOARD_PASSWORD": "new"}):
            self.assertFalse(app.valid_session(token, now=1001))
        self.assertFalse(app.valid_session("123.forged", now=1))

    def test_cron_needs_secret(self):
        self.assertEqual(self.call("POST", "/api/cron")[0], 401)
        self.assertEqual(self.call("POST", "/api/cron", {"authorization": "Bearer wrong"})[0], 401)
        with mock.patch.object(tracker, "check", return_value={"checked": 2, "answered": 2, "alerts": 0,
                                                               "exit_code": 0}) as chk:
            status, _, body = self.call("POST", "/api/cron", {"authorization": "Bearer s3cret"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["checked"], 2)
        self.assertFalse(chk.call_args.kwargs["summary"])
        with mock.patch.object(tracker, "check", return_value={}) as chk:
            self.call("GET", "/api/cron/summary", {"authorization": "Bearer s3cret"})
        self.assertTrue(chk.call_args.kwargs["summary"])

    def test_check_now_requires_login_and_skips_when_busy(self):
        self.assertEqual(dict(self.call("POST", "/check-now")[1])["Location"], "/login")
        auth = self.login()
        self.store.acquire_lock(60)
        other = store_mod.RedisStore(self.store.url, self.store.token, opener=self.store._open)
        with mock.patch.object(app, "get_store", return_value=other):
            status, headers, _ = self.call("POST", "/check-now", auth)
        self.assertEqual(dict(headers)["Location"], "/?checked=busy")

    def test_setup_page_lists_missing_pieces(self):
        status, _, body = self.call("GET", "/setup", self.login())
        self.assertEqual(status, 200)
        self.assertIn(b"Telegram alerts", body)
        self.assertIn(b"QStash not connected", body)

    def test_schedule_request(self):
        sent = {}

        def opener(req, timeout=None):
            sent["url"], sent["headers"] = req.full_url, dict(req.header_items())
            return io.BytesIO(b'{"scheduleId":"flight-tracker-check"}')
        with mock.patch.dict(os.environ, {"QSTASH_TOKEN": "q"}):
            app.create_schedule("https://my-app.vercel.app", opener)
        self.assertEqual(sent["url"], "https://qstash.upstash.io/v2/schedules/https://my-app.vercel.app/api/cron")
        self.assertEqual(sent["headers"]["Upstash-cron"], "*/10 * * * *")
        self.assertEqual(sent["headers"]["Upstash-forward-authorization"], "Bearer s3cret")


if __name__ == "__main__":
    unittest.main()
