# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import forecast as f


def cfg(delivery=None, **settings):
    line = {"name": "Widget", "match": r"^Widget (\d+)$"}
    if delivery:
        line["delivery_quarter"] = delivery
    return {"settings": {"buffer_pct": 10, **settings}, "lines": [line]}


def quarters(vals):
    return {(y, q): {"Widget 6": v} for y, q, v in vals}


TWO_YEARS = [(2022, 4, 5), (2023, 1, 5), (2023, 2, 20), (2023, 3, 10),    # period 2023 = 40 (T2+T3 = 30)
             (2023, 4, 5), (2024, 1, 5), (2024, 2, 10), (2024, 3, 10)]    # period 2024 = 30 (T2+T3 = 20)


def run(qs, stock=None, hold=None, on_order=None, delivery=None, partial=(), **settings):
    rows, periods, pinfo, line_info, meta = f.compute(cfg(delivery, **settings), qs, set(partial),
                                                      stock or {}, on_order or {}, hold or {}, [])
    return rows[0], pinfo, line_info["Widget"]


class Helpers(unittest.TestCase):
    def test_ceil_ignores_float_noise(self):
        self.assertEqual(f.ceil_(10 * 1.1), 11)      # naive math.ceil gives 12
        self.assertEqual(f.ceil_(35 * 1.1), 39)      # 38.5 -> 39
        self.assertEqual(f.ceil_(0), 0)

    def test_period_mapping(self):
        self.assertEqual(f.period_of(2025, 4), 2026)  # T4 starts the next period
        self.assertEqual(f.period_of(2026, 3), 2026)
        self.assertEqual(f.period_label(2026), "2025T4-2026T3")

    def test_size_order(self):
        sizes = ["XL", "10", "S", "04", "M", "18/S", "L"]
        self.assertEqual(sorted(sizes, key=f.size_key), ["S", "M", "L", "XL", "04", "10", "18/S"])

    def test_window_after(self):
        self.assertEqual(f.window_after((2026, 1), 4), [(2026, 2), (2026, 3)])   # yearly order run
        self.assertEqual(f.window_after((2026, 2), 4), [(2026, 3)])
        self.assertEqual(f.window_after((2026, 3), 4), [])                       # run after Q3
        self.assertEqual(f.window_after((2026, 4), 4), [(2027, 1), (2027, 2), (2027, 3)])
        self.assertEqual(f.window_after((2026, 3), 2), [(2026, 4), (2027, 1)])   # delivery in Q2

    def test_stock_file_key_prefers_filename_date(self):
        a, b = Path("StockReport_2026_09_01_0710.csv"), Path("StockReport_2026_10_05_0900.csv")
        self.assertLess(f.stock_file_key(a), f.stock_file_key(b))


class Compute(unittest.TestCase):
    def test_max_plus_buffer_minus_stock(self):
        it, _, li = run(quarters(TWO_YEARS), {"Widget 6": 15}, {"Widget 6": 5})
        self.assertEqual(it["target"], 44)          # 40 * 1.1
        self.assertEqual(it["available"], 20)
        self.assertEqual(it["order"], 24)
        self.assertEqual(li["n"], 2)

    def test_on_order_counts_as_stock(self):
        it, *_ = run(quarters(TWO_YEARS), {"Widget 6": 15}, on_order={"Widget 6": 20})
        self.assertEqual(it["order"], 9)            # 44 - (15 + 20)
        it, *_ = run(quarters(TWO_YEARS), {"Widget 6": 15}, on_order={"Widget 6": 20}, include_on_order=False)
        self.assertEqual(it["order"], 29)

    def test_partial_period_only_floors_the_max(self):
        qs = quarters([(2022, 4, 5), (2023, 1, 5), (2023, 2, 5), (2023, 3, 5), (2023, 4, 50)])
        it, _, li = run(qs)
        self.assertEqual(it["avg"], 20)             # partial excluded from the average
        self.assertEqual(it["target"], 55)          # but counted as a floor on the max
        self.assertEqual(li["n"], 1)

    def test_partial_quarter_flag_marks_period_partial(self):
        _, pinfo, li = run(quarters(TWO_YEARS), partial=["2024Q3"])
        self.assertFalse(pinfo[2024]["complete"])
        self.assertEqual(li["n"], 1)

    def test_unlaunched_years_are_not_zeroes(self):
        qs = {}
        for y, q in [(2022, 4), (2023, 1), (2023, 2), (2023, 3)]:
            qs[(y, q)] = {"Widget 6": 0}
        for y, q in [(2023, 4), (2024, 1), (2024, 2), (2024, 3)]:
            qs[(y, q)] = {"Widget 6": 10}
        it, _, li = run(qs)
        self.assertIsNone(it["vals"][2023])
        self.assertEqual(li["n"], 1)

    def test_may_run_deducts_terms_2_and_3(self):
        # data to 2025Q1, order arrives Term 4 -> T2+T3 still to sell: past years 30 and 20 -> avg 25 + unc 5
        qs = quarters(TWO_YEARS + [(2024, 4, 5), (2025, 1, 5)])
        it, _, li = run(qs, {"Widget 6": 50}, delivery=4)
        self.assertEqual(li["window"], [(2025, 2), (2025, 3)])
        self.assertEqual([u for _, u in it["inst"]], [20, 30])
        self.assertEqual(it["expected"], 30)
        self.assertEqual(it["order"], 24)           # 44 - (50 - 30)

    def test_october_run_deducts_nothing(self):
        it, _, li = run(quarters(TWO_YEARS), {"Widget 6": 50}, delivery=4)
        self.assertEqual(li["window"], [])
        self.assertEqual(it["expected"], 0)
        self.assertEqual(it["order"], 0)

    def test_total_summary_row_is_excluded(self):
        c = {"settings": {}, "lines": [{"name": "All", "match": r"^(.*)$"}]}
        qs = {(2022, 4): {"Widget 6": 5, "Total:": 999}}
        rows, *_ = f.compute(c, qs, set(), {"Total:": 0, "Widget 6": 1}, {}, {}, [])
        self.assertEqual([r["name"] for r in rows], ["Widget 6"])


if __name__ == "__main__":
    unittest.main()
