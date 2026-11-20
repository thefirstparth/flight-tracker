"""Offline tests for the dashboard builder. Run: python -m unittest -v"""
import json
import re
import tempfile
import unittest
from pathlib import Path

import dashboard
import tracker

CONFIG = Path(__file__).with_name("watches.toml")


class Dashboard(unittest.TestCase):
    def test_demo_page_builds_and_is_self_contained(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = dashboard.build(CONFIG, Path(tmp), Path(tmp) / "demo.html", demo=True)
            html = out.read_text(encoding="utf-8")
        self.assertNotIn(dashboard.DATA_MARKER, html)
        self.assertNotIn(dashboard.NOSCRIPT_MARKER, html)
        # No scripts, styles, images or fonts from anywhere else.
        self.assertIsNone(re.search(r'<(script|link|img|iframe)[^>]+(src|href)="https?:', html))
        data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1))
        self.assertTrue(data["demo"])
        self.assertEqual(len(data["watches"]), len(tracker.load_config(CONFIG)[1]))

    def test_demo_cheapest_is_never_above_exact_flights(self):
        data = dashboard.demo_data(CONFIG)
        for trip in data["trips"]:
            ws = [w for w in data["watches"] if w["id"] in trip["watches"]]
            for cheap in (w for w in ws if not w["flights"]):
                for exact in (w for w in ws if w["flights"] and w["search"] == cheap["search"]):
                    if exact["state"]["last"] is not None:
                        self.assertLessEqual(cheap["state"]["last"], exact["state"]["last"])

    def test_text_cannot_break_out_of_the_data_script(self):
        data = {"watches": [], "trips": [], "searches": {}, "events": [],
                "note": "</script><script>alert(1)</script><!--"}
        html = dashboard.render(data)
        body = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1)
        self.assertEqual(json.loads(body)["note"], data["note"])

    def test_empty_data_dir_still_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = dashboard.build(CONFIG, Path(tmp), Path(tmp) / "index.html")
            self.assertIn("Flight Prices", out.read_text(encoding="utf-8"))

    def test_history_is_compressed_to_changes(self):
        rows = [{"checked_at": f"t{i}", "price": p} for i, p in enumerate(["100", "100", "90", "", "", "90"])]
        self.assertEqual(dashboard._compress(rows), [["t0", 100.0], ["t2", 90.0], ["t3", None], ["t5", 90.0]])


if __name__ == "__main__":
    unittest.main()
