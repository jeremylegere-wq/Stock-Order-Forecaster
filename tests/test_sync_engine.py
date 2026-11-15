# SPDX-License-Identifier: GPL-3.0-or-later
import json
import tempfile
import threading
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import engine as eng
import forecast as f
import store
import sync


class AutoPartial(unittest.TestCase):
    def test_unfinished_term_is_partial(self):
        w = []
        qs = {(2026, 3): {}, (2026, 4): {}}
        self.assertEqual(f.auto_partial(qs, w, today=date(2026, 11, 13)), {"2026Q4"})
        self.assertEqual(f.auto_partial(qs, [], today=date(2027, 1, 2)), set())
        self.assertEqual(f.auto_partial(qs, [], today=date(2026, 12, 31)), {"2026Q4"})   # last day still counts as open


class Simulation(unittest.TestCase):
    def test_more_stock_means_less_risk(self):
        a = [35, 31, 25]
        _, p0 = eng.simulate(a, 20, 0)
        _, p1 = eng.simulate(a, 20, 20)
        _, p2 = eng.simulate(a, 20, 60)
        self.assertGreater(p0, p1)
        self.assertGreaterEqual(p1, p2)
        self.assertEqual(p2, 0.0)

    def test_repeatable(self):
        self.assertEqual(eng.simulate([10, 20, 30], 5, 5)[1], eng.simulate([10, 20, 30], 5, 5)[1])


def demo_root() -> Path:
    """A throw-away copy of the project's data frozen at the demo stock report."""
    import shutil
    src = Path(__file__).resolve().parent.parent
    tmp = Path(tempfile.mkdtemp())
    (tmp / "data" / "stock").mkdir(parents=True)
    shutil.copy(src / "config.toml", tmp / "config.toml")
    shutil.copytree(src / "data" / "sales", tmp / "data" / "sales")
    shutil.copy(src / "data" / "stock" / "StockReport_2026_10_05_0900.csv", tmp / "data" / "stock")
    shutil.copy(src / "data" / "holding.csv", tmp / "data")
    shutil.copy(src / "data" / "enrolments.csv", tmp / "data")
    return tmp


class DemoData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = demo_root()
        cls.d = eng.load_all(cls.root)

    def test_baseline_numbers_unchanged(self):
        res = eng.run(self.d)
        self.assertEqual(int((res.items["order"] > 0).sum()), 9)
        self.assertEqual(int(res.items["order"].sum()), 374)

    def test_delivery_scenario_raises_the_order(self):
        base = eng.run(self.d)
        res = eng.run(self.d, delivery={"Fleece Hoodie": 2})
        pick = lambda r: int(r.items[r.items["line"] == "Fleece Hoodie"]["order"].sum())
        self.assertEqual(pick(base), 95)
        self.assertEqual(pick(res), 134)

    def test_buffer_slider_raises_targets(self):
        lo, hi = eng.run(self.d, buffer_pct=0), eng.run(self.d, buffer_pct=30)
        self.assertGreater(hi.items["target"].sum(), lo.items["target"].sum())

    def test_per_100_students_uses_enrolments(self):
        res = eng.run(self.d)
        t = eng.per_100_students(res, self.d, "Basic Tee")
        self.assertEqual(len(t), 4)
        row = t[t["year"] == 2024].iloc[0]
        self.assertEqual(row["students"], 445)
        self.assertAlmostEqual(row["per100"], row["units"] / 445 * 100)


# --- a pretend the POS server -----------------------------------------------------------------
TX = [{"Id": i, "DateTime": "2027-02-%02dT10:00:00" % (i % 27 + 1), "StatusId": 1 if i != 3 else 9,
       "TransactionItems": [{"ProductId": 1, "Quantity": 2}, {"ProductId": 2, "Quantity": 1}]} for i in range(1, 6)]
TX.append({"Id": 99, "DateTime": "2027-05-01T10:00:00", "TransactionItems": [{"ProductId": 1, "Quantity": 50}]})  # wrong term
PRODUCTS = [{"Id": 1, "Name": "Basic Tee S", "CostPrice": 12.5}, {"Id": 2, "Name": "Trucker Cap", "CostPrice": 4}]
STOCK = [{"ProductId": 1, "LocationId": 1, "CurrentStock": 10, "OnOrder": 5},
         {"ProductId": 1, "LocationId": 2, "CurrentStock": 3, "OnOrder": 0},
         {"ProductId": 2, "LocationId": 1, "CurrentStock": 7, "OnOrder": 0}]


class Mock(BaseHTTPRequestHandler):
    auth_seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        from urllib.parse import parse_qs, urlparse
        u = urlparse(self.path)
        q = parse_qs(u.query)
        Mock.auth_seen.append(self.headers.get("Authorization"))
        if self.headers.get("Authorization", "").startswith("Basic ") is False:
            self.send_response(401); self.end_headers(); return
        data = {"/api/V4/Transaction": TX, "/api/V4/Product": PRODUCTS, "/api/V4/ProductStock": STOCK}.get(u.path)
        if data is None:
            self.send_response(404); self.end_headers(); return
        page = int(q.get("page", ["1"])[0])
        size = 2                                   # tiny pages to exercise paging
        body = json.dumps(data[(page - 1) * size: page * size]).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(body)


class SyncAgainstMock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Mock)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        (Path(cls.tmp.name) / "data").mkdir(parents=True)
        cls._root = sync.ROOT
        for fn in ("config.toml",):
            (Path(cls.tmp.name) / fn).write_text((cls._root / fn).read_text())
        sync.ROOT = Path(cls.tmp.name)
        cls.cfg = sync.load_cfg(cls._root / "sync.toml")
        cls.cfg["api"]["base_url"] = f"http://127.0.0.1:{cls.srv.server_port}/api/V4"
        cls.cfg["api"]["page_size"] = 2
        cls.client = sync.Client(cls.cfg, {"EPOS_API_KEY": "k", "EPOS_API_SECRET": "s"})

    @classmethod
    def tearDownClass(cls):
        sync.ROOT = cls._root
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def db(self):
        return store.open_db(Path(self.tmp.name) / "data" / "stock.db")

    def test_pull_sales_pages_filters_term_and_skips_voided(self):
        self.cfg["transactions"].update(status_field="StatusId", skip_status=[9])
        msg = sync.pull_sales(2027, 1, self.client, self.cfg, today=date(2027, 4, 10))
        got = {r["name"]: r["qty"] for r in self.db().execute("SELECT p.name, s.qty FROM sales s JOIN products p USING(product_id) WHERE year=2027 AND term=1")}
        # 5 transactions, #3 voided -> 4 valid, each 2 tees + 1 cap; the May sale (50) is a different term
        self.assertEqual(got, {"Basic Tee S": 8, "Trucker Cap": 4})
        self.assertIn("1 voided", msg)
        self.assertEqual(self.db().execute("SELECT source FROM terms WHERE year=2027 AND term=1").fetchone()[0], "epos_api")

    def test_open_term_is_flagged(self):
        msg = sync.pull_sales(2027, 1, self.client, self.cfg, today=date(2027, 3, 1))
        self.assertIn("not finished", msg)

    def test_pull_stock_sums_locations(self):
        sync.pull_stock(self.client, self.cfg)
        inp = store.load_inputs(self.db())
        self.assertEqual(inp["stock"]["Basic Tee S"], 13)
        self.assertEqual(inp["on_order"]["Basic Tee S"], 5)
        self.assertEqual(inp["stock"]["Trucker Cap"], 7)
        self.assertEqual(inp["costs"]["Basic Tee S"], 12.5)

    def test_bad_key_gives_plain_message(self):
        bad = sync.Client(self.cfg, {"EPOS_API_KEY": "k", "EPOS_API_SECRET": "s"})
        bad.headers["Authorization"] = "nope"
        with self.assertRaises(sync.SyncError) as cm:
            bad.get("Product", {"page": 1})
        self.assertIn("rejected", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
