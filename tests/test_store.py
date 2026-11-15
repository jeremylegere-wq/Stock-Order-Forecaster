# SPDX-License-Identifier: GPL-3.0-or-later
import csv
import io
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import store
from test_sync_engine import demo_root

SALES = b"ProductID,Name,Barcode,Brand,OrderCode,Qty,ValueIncVAT,TotCost\n101,Fleece Hoodie M,111,Demo Apparel Co,X1,7,203.00,132.65\n102,Trucker Cap,222,Demo Goods,X2,3,60.00,27.75\n,Total:,,,,10,263.00,160.40\n"
STOCK = b"Name,Barcode,Brand,Supplier,OrderCode,CategoryName,MeasuredCurrentStock,TotalStock,MeasuredOnOrder,MeasuredCostPrice,MeasuredSalePrice\nTrucker Cap,222,Demo,Demo Goods,X2,HATS,5,5.0,0,9.25,20\nBasic Tee XL,,Demo,Demo,X3,APPAREL,-6,-6.0,15,19.55,29.5\nTotal:,,,,,,,,,,\n"


def fresh():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "data").mkdir()
    shutil.copy(Path(__file__).resolve().parent.parent / "config.toml", tmp / "config.toml")
    return tmp, store.open_db(tmp / "data" / "stock.db")


class Imports(unittest.TestCase):
    def test_sales_are_split_into_parts(self):
        tmp, c = fresh()
        r = store.import_sales(c, SALES, 2027, 1, file_name="x.csv", root=tmp, backup=False)
        self.assertEqual((r["status"], r["units"]), ("added", 10))                    # the Total: row is not counted
        p = c.execute("SELECT line, size_label, epos_id, brand FROM products WHERE name='Fleece Hoodie M'").fetchone()
        self.assertEqual((p["line"], p["size_label"], p["epos_id"], p["brand"]), ("Fleece Hoodie", "M", 101, "Demo Apparel Co"))
        t = c.execute("SELECT period_start, period_end, source FROM terms").fetchone()
        self.assertEqual(tuple(t), ("2027-01-01", "2027-03-31", "epos_export"))
        s = c.execute("SELECT qty, value_inc_vat, total_cost FROM sales").fetchall()
        self.assertEqual(sorted(tuple(x) for x in s), [(3, 60.0, 27.75), (7, 203.0, 132.65)])

    def test_same_file_twice_is_ignored(self):
        tmp, c = fresh()
        store.import_sales(c, SALES, 2027, 1, root=tmp, backup=False)
        r = store.import_sales(c, SALES, 2027, 1, root=tmp, backup=False)
        self.assertEqual(r["status"], "unchanged")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM sales").fetchone()[0], 2)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM imports").fetchone()[0], 1)

    def test_new_file_for_same_term_replaces_only_that_term(self):
        tmp, c = fresh()
        store.import_sales(c, SALES, 2026, 4, root=tmp, backup=False)
        store.import_sales(c, SALES, 2027, 1, root=tmp, backup=False)
        newer = SALES.replace(b",7,203", b",9,261")
        r = store.import_sales(c, newer, 2027, 1, root=tmp, backup=False)
        self.assertEqual(r["status"], "replaced")
        self.assertEqual(c.execute("SELECT units FROM terms WHERE year=2027").fetchone()[0], 12)
        self.assertEqual(c.execute("SELECT units FROM terms WHERE year=2026").fetchone()[0], 10)       # untouched
        self.assertEqual(c.execute("SELECT COUNT(*) FROM imports WHERE superseded=1").fetchone()[0], 1)  # old load kept in the log

    def test_real_export_beats_seeded_copy(self):
        tmp, c = fresh()
        store.import_sales(c, SALES, 2022, 1, source="epos_export", root=tmp, backup=False)
        r = store.import_sales(c, SALES.replace(b",7,", b",8,"), 2022, 1, source="seeded", root=tmp, backup=False)
        self.assertEqual(r["status"], "skipped")

    def test_bad_files_get_plain_messages(self):
        tmp, c = fresh()
        with self.assertRaises(store.StoreError) as e:
            store.import_sales(c, b"Foo,Bar\n1,2\n", 2027, 1, root=tmp, backup=False)
        self.assertIn("missing column", str(e.exception))
        with self.assertRaises(store.StoreError):
            store.import_sales(c, SALES, 2027, 5, root=tmp, backup=False)

    def test_stock_snapshots_accumulate_and_negatives_are_clamped(self):
        tmp, c = fresh()
        r = store.import_stock(c, STOCK, file_name="StockReport_2026_11_13_0544.csv", root=tmp, backup=False)
        self.assertEqual((r["status"], r["negatives"]), ("added", 1))
        self.assertEqual(store.import_stock(c, STOCK, file_name="again.csv", root=tmp, backup=False)["status"], "unchanged")
        older = STOCK.replace(b"5.0", b"9.0")
        store.import_stock(c, older, file_name="StockReport_2026_10_05_0900.csv", root=tmp, backup=False)
        inp = store.load_inputs(c)
        self.assertEqual(len(inp["snapshots"]), 2)
        self.assertEqual(inp["latest"]["taken_at"], "2026-11-13T05:44")           # newest by date, not by load order
        self.assertEqual(inp["stock"]["Basic Tee XL"], 0)
        self.assertEqual(inp["negatives"], {"Basic Tee XL": -6})
        self.assertEqual(inp["on_order"]["Basic Tee XL"], 15)

    def test_confirming_a_snapshot(self):
        tmp, c = fresh()
        store.import_stock(c, STOCK, file_name="StockReport_2026_11_13_0544.csv", root=tmp, backup=False)
        r = store.import_stock(c, STOCK, file_name="StockReport_2026_11_13_0544.csv", confirmed=True, confirmed_by="Sam", root=tmp, backup=False)
        self.assertIn("confirmed", r["message"])
        self.assertTrue(store.load_inputs(c)["latest"]["confirmed"])


class EnrolmentsAndNotes(unittest.TestCase):
    def test_enrolments_totals_and_grid(self):
        tmp, c = fresh()
        store.set_enrolments(c, [{"year": 2021, "year_level": "Prep", "students": 10}, {"year": 2021, "year_level": "Year 1", "students": 20},
                                 {"year": 2023, "year_level": "Total", "students": 860}, {"year": 2024, "year_level": "Prep", "students": "N/A"}],
                             root=tmp, backup=False)
        self.assertEqual(store.enrolment_totals(c), {2021: 30, 2023: 860})
        grid = {r["Year level"]: r for r in store.get_enrolment_grid(c)}
        self.assertEqual((grid["Prep"]["2021"], grid["1"]["2021"], grid["Total"]["2023"]), (10, 20, 860))

    def test_annotations(self):
        tmp, c = fresh()
        store.add_annotation(c, 2023, 2, "Hoodies sold out early", root=tmp, backup=False)
        self.assertEqual(store.get_annotations(c)[0]["term"], 2)
        with self.assertRaises(store.StoreError):
            store.add_annotation(c, 2023, None, "  ", root=tmp, backup=False)


class ExportRestore(unittest.TestCase):
    def test_migration_matches_the_folders_and_survives_export_and_restore(self):
        root = demo_root()
        c = store.ensure_ready(root)
        inp1 = store.load_inputs(c)
        self.assertEqual(len(inp1["quarters"]), 16)
        # a full export, then rebuild a brand new database from it
        z = root / "backup.zip"
        z.write_bytes(store.export_zip_bytes(c))
        names = set(zipfile.ZipFile(z).namelist())
        self.assertTrue({"sales_history.csv", "stock_levels.csv", "enrolments.csv", "README.txt"} <= names)
        c2 = store.open_db(root / "restored.db")
        store.restore(c2, z)
        inp2 = store.load_inputs(c2)
        self.assertEqual(inp1["quarters"], inp2["quarters"])
        self.assertEqual(inp1["stock"], inp2["stock"])
        self.assertEqual(inp1["holding"], inp2["holding"])
        self.assertEqual(inp1["enrolments"], inp2["enrolments"])
        self.assertEqual(store.status(c)["units"], store.status(c2)["units"])
        # and the forecast from the restored copy is identical
        import forecast as f
        cfg = f.load_config(root / "config.toml")
        args = lambda i: (cfg, i["quarters"], set(), i["stock"], i["on_order"], i["holding"], [])
        a = f.compute(*args(inp1))[0]
        b = f.compute(*args(inp2))[0]
        self.assertEqual([(x["name"], x["order"]) for x in a], [(x["name"], x["order"]) for x in b])

    def test_sales_history_csv_is_tidy(self):
        root = demo_root()
        c = store.ensure_ready(root)
        rows = list(csv.DictReader(io.StringIO(store.export_texts(c)["sales_history.csv"])))
        self.assertEqual(rows[0].keys() >= {"term_label", "product", "product_line", "size", "qty", "source"}, True)
        self.assertEqual(sum(int(r["qty"]) for r in rows), store.status(c)["units"])

    def test_restore_refuses_a_non_empty_database(self):
        root = demo_root()
        c = store.ensure_ready(root)
        z = root / "b.zip"
        z.write_bytes(store.export_zip_bytes(c))
        with self.assertRaises(store.StoreError):
            store.restore(c, z)

    def test_backup_goes_to_the_configured_folder(self):
        tmp, c = fresh()
        target = tmp / "sharepoint_sync"
        cfg = (tmp / "config.toml").read_text().replace('backup_dir = ""', f'backup_dir = "{target}"')
        (tmp / "config.toml").write_text(cfg)
        r = store.import_sales(c, SALES, 2027, 1, root=tmp)
        self.assertIn(str(target), r["backup"])
        self.assertTrue((target / "sales_history.csv").exists())


if __name__ == "__main__":
    unittest.main()
