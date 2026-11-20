"""Offline tests for the tracker (no network). Run: python -m unittest -v"""
import tempfile
import unittest
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
        answers = iter([[], [Picking.opts[0]], [Picking.opts[1]]])
        calls = []

        def fake(key, settings):
            calls.append(1)
            return next(answers)
        got = t.fetch_options(("BLR", "SIN", "d", "r"), S, {("6E 1457", "6E 1458")},
                              search=fake, sleep=lambda s: None)
        self.assertEqual(len(calls), 3)
        self.assertEqual([o.price for o in got], [26576, 28230])

    def test_gives_up_with_none(self):
        got = t.fetch_options(("BLR", "SIN", "d", "r"), S, set(),
                              search=lambda k, s: [], sleep=lambda s: None)
        self.assertIsNone(got)


if __name__ == "__main__":
    unittest.main()
