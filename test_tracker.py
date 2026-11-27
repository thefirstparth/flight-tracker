"""Offline tests for the tracker (no network). Run: python -m unittest -v"""
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import tracker as t

S = dict(t.DEFAULT_SETTINGS)


def watch(**kw):
    base = dict(id="w", name="W", origin="BLR", destination="SIN", depart="2027-03-02",
                ret="2027-03-07", flights=None, target=None)
    base.update(kw)
    return t.Watch(**base)


class Rules(unittest.TestCase):
    def step(self, st, price, target=None):
        return t.evaluate(st, price, ("6E 1",), target, S, "now")

    def test_first_check_is_baseline(self):
        st, ev = self.step(None, 28230)
        self.assertEqual([k for k, _ in ev], ["baseline"])
        self.assertEqual((st["low"], st["ref"], st["last"]), (28230, 28230, 28230))

    def test_small_wobble_is_quiet(self):
        st, _ = self.step(None, 28230)
        st, ev = self.step(st, 27900)  # -330: below both ₹500 and 3%
        self.assertEqual(ev, [])

    def test_drop_uses_larger_of_rupees_and_percent(self):
        st, _ = self.step(None, 30000)  # 3% = 900 > 500
        st, ev = self.step(st, 29200)
        self.assertEqual(ev, [])
        st, ev = self.step(st, 29100)
        self.assertEqual([k for k, _ in ev], ["drop"])
        self.assertTrue(ev[0][1]["new_low"])
        self.assertEqual(st["ref"], 29100)

    def test_slow_slide_alerts_once_it_adds_up(self):
        st, _ = self.step(None, 20000)
        for p in (19800, 19600):
            st, ev = self.step(st, p)
            self.assertEqual(ev, [])
        st, ev = self.step(st, 19400)  # -600 vs ref 20000
        self.assertEqual([k for k, _ in ev], ["drop"])

    def test_rise_then_fall_alerts_both_ways(self):
        st, _ = self.step(None, 18000)
        st, ev = self.step(st, 25000)
        self.assertEqual([k for k, _ in ev], ["rise"])
        st, ev = self.step(st, 21000)
        self.assertEqual([k for k, _ in ev], ["drop"])
        self.assertFalse(ev[0][1]["new_low"])

    def test_target_alerts_when_crossed_not_repeatedly(self):
        st, _ = self.step(None, 28000, target=26000)
        st, ev = self.step(st, 25900, target=26000)
        self.assertIn("target", [k for k, _ in ev])
        st, ev = self.step(st, 25800, target=26000)
        self.assertNotIn("target", [k for k, _ in ev])

    def test_target_already_met_on_first_check(self):
        _, ev = self.step(None, 25000, target=26000)
        self.assertEqual([k for k, _ in ev], ["baseline", "target"])

    def test_missing_alerts_once_then_back(self):
        st, _ = self.step(None, 20000)
        kinds = []
        for _ in range(4):
            st, ev = self.step(st, None)
            kinds += [k for k, _ in ev]
        self.assertEqual(kinds, ["missing"])
        st, ev = self.step(st, 20100)
        self.assertEqual([k for k, _ in ev], ["back"])
        self.assertEqual(st["misses"], 0)


class BookOrWait(unittest.TestCase):
    from datetime import date as _d
    TODAY = _d(2026, 11, 27)

    def g(self, now, low=22500, high=37000, history=None):
        hist = history if history is not None else [["2026-10-%02d" % (i + 1), 40000] for i in range(28)]
        return {"now": now, "low": low, "high": high, "typical": 26000, "history": hist, "suggestions": []}

    def test_below_range_and_near_low_says_book(self):
        hist = [["d", 30000.0]] * 30
        r = t.book_or_wait(self.g(21000, history=hist), None, "2027-03-02", self.TODAY)
        self.assertEqual(r["verdict"], "book")

    def test_far_above_range_with_time_says_wait(self):
        hist = [["d", 30000.0]] * 30
        r = t.book_or_wait(self.g(45000, history=hist), None, "2027-06-01", self.TODAY)
        self.assertEqual(r["verdict"], "wait")
        self.assertTrue(any("above Google's typical range" in x["text"] for x in r["reasons"]))

    def test_peak_dates_and_closing_window_pull_toward_booking(self):
        comparison = {"yours": 43690, "similar": [{"depart": d, "price": 29000} for d in
                                                   ("2027-01-01", "2027-01-08", "2027-01-29", "2027-02-05")]}
        r = t.book_or_wait(self.g(43690), comparison, "2027-01-22", self.TODAY)
        self.assertTrue(any("peak" in x["text"] for x in r["reasons"]))
        self.assertLessEqual(r["score"], 60)

    def test_whole_season_high_is_explained_not_contradicted(self):
        # The live Singapore case: above Google's range, but every nearby week is dearer still.
        comparison = {"yours": 26576, "similar": [{"depart": d, "price": p} for d, p in (
            ("2027-02-02", 26576), ("2027-02-09", 31000), ("2027-02-16", 30400), ("2027-02-23", 28200),
            ("2027-03-02", 26576), ("2027-03-09", 29700), ("2027-03-16", 29700), ("2027-03-23", 29700),
            ("2027-03-30", 26800))]}
        hist = [["d", 26000.0]] * 30
        plain = t.book_or_wait(self.g(26576, 17000, 25500, hist), None, "2027-03-02", self.TODAY)
        r = t.book_or_wait(self.g(26576, 17000, 25500, hist), comparison, "2027-03-02", self.TODAY)
        self.assertIn("whole season", r["summary"])
        self.assertTrue(any("cheaper than the same trip in other weeks" in x["text"] for x in r["reasons"]))
        self.assertLess(r["score"], plain["score"])

    def test_last_three_weeks(self):
        r = t.book_or_wait(self.g(30000), None, "2026-12-10", self.TODAY)
        self.assertTrue(any("last three weeks" in x["text"] for x in r["reasons"]))

    def test_no_google_data_no_verdict(self):
        self.assertIsNone(t.book_or_wait(None, None, "2027-01-22", self.TODAY))
        self.assertIsNone(t.book_or_wait(self.g(30000), None, "2026-11-01", self.TODAY))  # departed

    def test_parse_price_insight(self):
        inner = [None] * 7
        inner[5] = [5, [None, 43690], [None, 26284], [None, -17406], [None, 22500], [None, 37000], 1, None, None, None,
                    [[[1790533800000, 41422], [1790620200000, 41441]]], None, "Phuket"]
        inner[6] = [[["2027-01-20", "2027-01-25", [[None, 30991], "tok"], [2, 1]]]]
        g = t.parse_price_insight(inner)
        self.assertEqual((g["now"], g["low"], g["high"], g["typical"]), (43690, 22500, 37000, 26284))
        self.assertEqual(len(g["history"]), 2)
        self.assertEqual(g["suggestions"][0], {"depart": "2027-01-20", "return": "2027-01-25", "price": 30991.0})
        self.assertIsNone(t.parse_price_insight([None] * 4))
        bad = list(inner); bad[5] = [5, [None, 1], None, None, [None, 9], [None, 3]]
        self.assertIsNone(t.parse_price_insight(bad))


class Parsing(unittest.TestCase):
    def test_flight_normalization(self):
        for raw in ("6E 1457", "6e1457", "6E-1457", " 6E 01457 "):
            self.assertEqual(t.normalize_flight(raw), "6E 1457")
        with self.assertRaises(t.ConfigError):
            t.normalize_flight("IndiGo")

    def test_inr_uses_indian_grouping(self):
        self.assertEqual(t.inr(28230), "₹28,230")
        self.assertEqual(t.inr(123456), "₹1,23,456")
        self.assertEqual(t.inr(950), "₹950")

    def test_shipped_watch_list_loads(self):
        settings, watches = t.load_config(Path(__file__).with_name("watches.toml"))
        self.assertTrue(settings["direct_only"])
        self.assertGreaterEqual(len(watches), 1)
        self.assertEqual(len({w.id for w in watches}), len(watches))

    def test_flight_count_must_match_trip(self):
        bad = '[[watch]]\nname="x"\nfrom="BLR"\nto="SIN"\ndepart="2027-03-02"\nflights=["6E 1","6E 2"]\n'
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write(bad)
        with self.assertRaises(t.ConfigError):
            t.load_config(Path(fh.name))


class Picking(unittest.TestCase):
    opts = [t.Option(26576, ("6E 1247", "6E 1248"), ("2 Mar 20:00", "7 Mar 01:00")),
            t.Option(28230, ("6E 1457", "6E 1458"), ("2 Mar 10:20", "7 Mar 19:30"))]

    def test_exact_flights(self):
        o = t.pick(watch(flights=("6E 1457", "6E 1458")), self.opts)
        self.assertEqual(o.price, 28230)

    def test_cheapest(self):
        self.assertEqual(t.pick(watch(), self.opts).price, 26576)

    def test_exact_missing(self):
        self.assertIsNone(t.pick(watch(flights=("QP 1", "QP 2")), self.opts))


class Retrying(unittest.TestCase):
    def test_retries_until_needed_flights_seen(self):
        answers = iter([[Picking.opts[0]], [Picking.opts[1]]])  # the needed pair shows up on the second try
        calls = []

        def fake(key, settings, needed):
            calls.append(1)
            return next(answers)
        got = t.fetch_options(("BLR", "SIN", "d", None), S, {("6E 1457", "6E 1458")},
                              search=fake, sleep=lambda s: None)
        self.assertEqual(len(calls), 2)
        self.assertEqual([o.price for o in got], [26576, 28230])

    def test_gives_up_with_none(self):
        got = t.fetch_options(("BLR", "SIN", "d", "r"), S, set(),
                              search=lambda k, s, n: [], sleep=lambda s: None)
        self.assertIsNone(got)


if __name__ == "__main__":
    unittest.main()


class DateWindows(unittest.TestCase):
    KEY = ("BLR", "IXD", "2027-10-23..2027-10-24", "2027-10-31..2027-11-01")

    def setUp(self):
        t._window_prices.clear(); t._unfinished.clear(); t._answered_empty.clear(); t._google_insights.clear()
        t._deadline = None
        p = mock.patch.object(t.time, "sleep")
        p.start(); self.addCleanup(p.stop)
        h = mock.patch.object(t, "BOOKING_HORIZON_DAYS", 100000)
        h.start(); self.addCleanup(h.stop)

    def test_beyond_booking_horizon_is_not_searched(self):
        with mock.patch.object(t, "BOOKING_HORIZON_DAYS", 0), mock.patch.object(t, "_cheapest_on") as page:
            self.assertEqual(t._search_windows(self.KEY, S), [])
        page.assert_not_called()
        self.assertIn(self.KEY, t._answered_empty)

    def test_cheapest_pair_then_its_flights(self):
        prices = {("2027-10-23", "2027-10-31"): 9000.0, ("2027-10-24", "2027-10-31"): 8000.0,
                  ("2027-10-23", "2027-11-01"): None, ("2027-10-24", "2027-11-01"): 8500.0}
        legs = ({"flight": "6E 6531", "dep": "2027-10-24T11:45"}, {"flight": "6E 6532", "dep": "2027-10-31T15:00"})
        with mock.patch.object(t, "_cheapest_on", side_effect=lambda o, d, dep, ret, s: prices[(dep, ret)]), \
                mock.patch.object(t, "_search_once", return_value=[t.Option(8000, ("6E 6531", "6E 6532"), ("a", "b"), legs)]) as once:
            got = t._search_windows(self.KEY, S)
        once.assert_called_once_with(("BLR", "IXD", "2027-10-24", "2027-10-31"), S, set())
        self.assertEqual(got[0].flights, ("6E 6531", "6E 6532"))
        self.assertEqual(len(t._window_prices[t.search_id(self.KEY)]), 4)

    def test_nothing_on_sale_is_an_answer(self):
        with mock.patch.object(t, "_cheapest_on", return_value=None):
            self.assertEqual(t._search_windows(self.KEY, S), [])
        self.assertIn(self.KEY, t._answered_empty)

    def test_refusal_stops_and_keeps_progress(self):
        calls = []

        def refuse_second(o, d, dep, ret, s):
            calls.append(dep)
            if len(calls) > 1:
                raise t.NotAnswered("blocked")
            return 9000.0
        with mock.patch.object(t, "_cheapest_on", side_effect=refuse_second):
            self.assertEqual(t._search_windows(self.KEY, S), [])
        self.assertNotIn(self.KEY, t._answered_empty)
        self.assertEqual(len(t._window_prices[t.search_id(self.KEY)]), 1)  # the first pair is remembered
