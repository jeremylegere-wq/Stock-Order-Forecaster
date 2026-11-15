#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
The stock planner database (SQLite) - one file, data/stock.db.

Everything the forecast needs lives here, broken into its parts:

  products          one row per product name, with its supplier, category, and the product
                    line and size worked out from config.toml
  terms             one row per term (quarter) of sales that has been loaded (where it came from)
  sales             units and dollars sold: one row per product per term
  stock_snapshots   each stock report that has been loaded, and whether the stock manager confirmed it
  stock_levels      stock on hand / on order / cost price per product per snapshot
  holdings          stock held outside Epos Now (e.g. by a supplier)
  enrolments        headcount / enrolments by group (e.g. school year level) - typed in on the dashboard
  annotations       notes on one-off events (new product, enrolment surge...) shown on the charts
  imports           an audit log of every file loaded (what, when, checksum, units)

Every sales report you load is ADDED here. Loading a term again replaces that term only, and the
old load stays in the audit log. After each load a full CSV export is written to the backup
folder in config.toml (point it at a synced shared folder (SharePoint, OneDrive...)), and the whole database can be
rebuilt from that export:   python store.py restore <export.zip>

Commands:
    python store.py status
    python store.py ingest                          load any new CSVs from data/sales, data/stock, holding.csv
    python store.py import-sales FILE.csv 2027Q1
    python store.py import-stock FILE.csv [--confirmed]
    python store.py export [FOLDER]                 write the full CSV export (and a zip)
    python store.py restore EXPORT.zip              rebuild an empty database from an export
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import sqlite3
import sys
import zipfile
from datetime import date, datetime
from pathlib import Path

import forecast as f

ROOT = f.ROOT
DB_PATH = ROOT / "data" / "stock.db"
SCHEMA_VERSION = 1
EXCLUDE_NAME = re.compile(r"^total:?$", re.I)

SCHEMA = """
CREATE TABLE products (
    product_id  INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,          -- exactly as Epos Now spells it
    epos_id     INTEGER,
    barcode     TEXT, brand TEXT, supplier TEXT, order_code TEXT, category TEXT,
    line        TEXT,                          -- product line, from config.toml
    size_raw    TEXT,                          -- the size as captured from the name
    size_label  TEXT                           -- friendly size label
);
CREATE TABLE imports (
    import_id   INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('sales','stock','holding','restore')),
    file_name   TEXT, sha256 TEXT, source TEXT,
    imported_at TEXT NOT NULL, rows INTEGER, units INTEGER, note TEXT,
    superseded  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE terms (
    year INTEGER NOT NULL, term INTEGER NOT NULL CHECK (term BETWEEN 1 AND 4),
    period_start TEXT NOT NULL, period_end TEXT NOT NULL,
    source      TEXT NOT NULL,                 -- epos_export | epos_api | seeded
    import_id   INTEGER REFERENCES imports(import_id),
    units       INTEGER NOT NULL,
    PRIMARY KEY (year, term)
);
CREATE TABLE sales (
    year INTEGER NOT NULL, term INTEGER NOT NULL,
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    qty INTEGER NOT NULL,
    value_inc_vat REAL, value_exc_vat REAL, discount REAL, total_cost REAL,
    PRIMARY KEY (year, term, product_id),
    FOREIGN KEY (year, term) REFERENCES terms(year, term) ON DELETE CASCADE
);
CREATE TABLE stock_snapshots (
    snapshot_id INTEGER PRIMARY KEY,
    taken_at    TEXT NOT NULL UNIQUE,          -- when the report was run
    import_id   INTEGER REFERENCES imports(import_id),
    confirmed   INTEGER NOT NULL DEFAULT 0,    -- 1 = the stock manager confirmed these figures (stocktake)
    confirmed_by TEXT, note TEXT
);
CREATE TABLE stock_levels (
    snapshot_id INTEGER NOT NULL REFERENCES stock_snapshots(snapshot_id) ON DELETE CASCADE,
    product_id  INTEGER NOT NULL REFERENCES products(product_id),
    total_stock REAL, on_order REAL, cost_price REAL, sale_price REAL,
    PRIMARY KEY (snapshot_id, product_id)
);
CREATE TABLE holdings (
    product_id INTEGER NOT NULL REFERENCES products(product_id),
    location   TEXT NOT NULL DEFAULT 'Held outside Epos Now',
    qty        INTEGER NOT NULL, as_of TEXT, note TEXT,
    PRIMARY KEY (product_id, location)
);
CREATE TABLE enrolments (                       -- headcount by group (e.g. school year level), typed in or imported
    year        INTEGER NOT NULL,
    year_level  TEXT NOT NULL,                  -- Prep, 1 .. 6, or Total
    students    INTEGER NOT NULL,
    source      TEXT, note TEXT,
    PRIMARY KEY (year, year_level)
);
CREATE TABLE annotations (                      -- notes about one-off events that explain a number
    annotation_id INTEGER PRIMARY KEY,
    year INTEGER NOT NULL, term INTEGER,        -- term may be blank for a whole-year note
    note TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX idx_sales_product ON sales(product_id);
"""


class StoreError(Exception):
    """A problem with a file or request, in words a person can act on."""


# --------------------------------------------------------------------------- connection
def open_db(path: Path | None = None, create: bool = True) -> sqlite3.Connection:
    path = Path(path or DB_PATH)
    new = not path.exists()
    if new and not create:
        raise StoreError(f"No database at {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    if ver == 0 and not _has_tables(conn):
        conn.executescript(SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    elif ver > SCHEMA_VERSION:
        raise StoreError("This database was made by a newer version of the program. Update the program files.")
    return conn


def _has_tables(conn) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='products'").fetchone())


def is_empty(conn) -> bool:
    return conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0] == 0


# --------------------------------------------------------------------------- helpers
def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def num(v, default=0.0) -> float:
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return default


def term_dates(year: int, term: int) -> tuple[str, str]:
    start = date(year, 3 * (term - 1) + 1, 1)
    return start.isoformat(), f.quarter_end((year, term)).isoformat()


def parse_csv(data: bytes) -> list[dict]:
    text = data.decode("utf-8-sig", errors="replace")
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        rows.append({(k or "").strip(): (v or "").strip() for k, v in row.items()})
    return rows


def _require(rows: list[dict], cols: list[str], what: str) -> None:
    have = set(rows[0].keys()) if rows else set()
    missing = [c for c in cols if c not in have]
    if not rows:
        raise StoreError(f"That {what} file has no rows.")
    if missing:
        raise StoreError(f"That doesn't look like an Epos Now {what} export (missing column: {', '.join(missing)}).")


def _log(conn, kind, file_name, digest, source, rows, units, note="") -> int:
    cur = conn.execute("INSERT INTO imports(kind,file_name,sha256,source,imported_at,rows,units,note) VALUES (?,?,?,?,?,?,?,?)",
                       (kind, file_name, digest, source, now(), rows, units, note))
    return cur.lastrowid


def _product_id(conn, name: str, **attrs) -> int:
    row = conn.execute("SELECT product_id FROM products WHERE name = ?", (name,)).fetchone()
    clean = {k: v for k, v in attrs.items() if v not in (None, "")}
    if row:
        if clean:
            sets = ", ".join(f"{k} = ?" for k in clean)
            conn.execute(f"UPDATE products SET {sets} WHERE product_id = ?", (*clean.values(), row[0]))
        return row[0]
    cols = ["name", *clean]
    cur = conn.execute(f"INSERT INTO products({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", (name, *clean.values()))
    return cur.lastrowid


def classify_all(conn, cfg: dict) -> None:
    """Work out each product's line and size from the patterns in config.toml."""
    patterns = [(ln, re.compile(ln["match"])) for ln in cfg["lines"]]
    excl = [re.compile(p, re.I) for p in cfg["settings"].get("exclude", [r"^total:?$"])]
    for r in conn.execute("SELECT product_id, name FROM products").fetchall():
        line = raw = label = None
        if not any(x.search(r["name"]) for x in excl):
            for ln, rx in patterns:
                m = rx.match(r["name"])
                if m:
                    raw = m.group(1) if m.groups() else r["name"]
                    line, label = ln["name"], ln.get("size_labels", {}).get(raw, raw)
                    break
        conn.execute("UPDATE products SET line=?, size_raw=?, size_label=? WHERE product_id=?", (line, raw, label, r["product_id"]))


def _config(root: Path) -> dict | None:
    try:
        return f.load_config(root / "config.toml")
    except SystemExit:
        return None


# --------------------------------------------------------------------------- import: sales
def import_sales(conn, data: bytes, year: int, term: int, *, source: str = "epos_export",
                 file_name: str = "", root: Path = ROOT, backup: bool = True) -> dict:
    """Add one term's 'Sales by Product' report. Returns a summary dict (status, units, notes...)."""
    if term not in (1, 2, 3, 4) or not 2000 <= year <= 2100:
        raise StoreError("Say which term this is, e.g. year 2027, term 1.")
    rows = parse_csv(data)
    _require(rows, ["Name", "Qty"], "Sales by Product")
    digest = sha(data)
    existing = conn.execute("SELECT t.source, t.units, t.import_id, i.sha256 FROM terms t LEFT JOIN imports i USING(import_id) "
                            "WHERE year=? AND term=?", (year, term)).fetchone()
    label = f"{year}Q{term}"
    if existing and existing["sha256"] == digest:
        return {"status": "unchanged", "term": label, "units": existing["units"],
                "message": f"{label} is already loaded from an identical file. Nothing changed."}
    if existing and existing["source"] != "seeded" and source == "seeded":
        return {"status": "skipped", "term": label, "units": existing["units"],
                "message": f"{label} already has a real Epos Now export. The old-spreadsheet copy was not used."}

    totals: dict[str, dict] = {}
    for r in rows:
        name = r.get("Name", "")
        if not name or EXCLUDE_NAME.match(name):
            continue
        t = totals.setdefault(name, {"qty": 0, "vi": 0.0, "ve": 0.0, "disc": 0.0, "cost": 0.0, "attrs": r})
        t["qty"] += int(round(num(r.get("Qty"))))
        t["vi"] += num(r.get("ValueIncVAT") or r.get("Value"))
        t["ve"] += num(r.get("ValueExcVAT"))
        t["disc"] += num(r.get("Discount"))
        t["cost"] += num(r.get("TotCost"))
    if not totals:
        raise StoreError("That file has no products in it.")
    units = sum(t["qty"] for t in totals.values())

    old_note = ""
    if existing:
        old_note = f"replaced {existing['units']:,} units from {existing['source']}"
        conn.execute("UPDATE imports SET superseded = 1 WHERE import_id = ?", (existing["import_id"],))
        conn.execute("DELETE FROM terms WHERE year=? AND term=?", (year, term))      # cascades to sales
    before = {r[0] for r in conn.execute("SELECT name FROM products")}
    imp = _log(conn, "sales", file_name, digest, source, len(totals), units, old_note)
    start, end = term_dates(year, term)
    conn.execute("INSERT INTO terms VALUES (?,?,?,?,?,?,?)", (year, term, start, end, source, imp, units))
    for name, t in totals.items():
        a = t["attrs"]
        pid = _product_id(conn, name, epos_id=int(num(a.get("ProductID"), 0)) or None, barcode=a.get("Barcode"),
                          brand=a.get("Brand"), order_code=a.get("OrderCode"))
        conn.execute("INSERT INTO sales VALUES (?,?,?,?,?,?,?,?)",
                     (year, term, pid, t["qty"], t["vi"] or None, t["ve"] or None, t["disc"] or None, t["cost"] or None))
    cfg = _config(root)
    if cfg:
        classify_all(conn, cfg)
    conn.commit()
    new = sorted(set(totals) - before)
    msg = f"Loaded {label}: {units:,} units across {len(totals)} products."
    if existing:
        msg += f" This replaced the earlier {label} ({existing['units']:,} units, {existing['source']}); the old load stays in the audit log."
    if new and before:
        msg += f" {len(new)} product(s) are new to the database."
    out = {"status": "replaced" if existing else "added", "term": label, "units": units, "products": len(totals),
           "new_products": new, "message": msg}
    if backup:
        out["backup"] = run_backup(conn, root)
    return out


# --------------------------------------------------------------------------- import: stock
def stock_taken_at(file_name: str, fallback: datetime | None = None) -> str:
    m = f.STOCKDATE.search(file_name or "")
    if m:
        hhmm = m.group(4) or "0000"
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), int(hhmm[:2]), int(hhmm[2:])).isoformat(timespec="minutes")
    return (fallback or datetime.now()).isoformat(timespec="minutes")


def import_stock(conn, data: bytes, *, file_name: str = "", taken_at: str | None = None, confirmed: bool = False,
                 confirmed_by: str = "", note: str = "", source: str = "epos_export", root: Path = ROOT, backup: bool = True) -> dict:
    """Add a 'Stock Levels' report as a new snapshot."""
    rows = parse_csv(data)
    _require(rows, ["Name", "TotalStock"], "Stock Levels")
    digest = sha(data)
    dup = conn.execute("SELECT i.import_id, s.snapshot_id, s.taken_at, s.confirmed FROM imports i "
                       "JOIN stock_snapshots s ON s.import_id = i.import_id WHERE i.sha256 = ? AND i.superseded = 0", (digest,)).fetchone()
    if dup:
        if confirmed and not dup["confirmed"]:
            conn.execute("UPDATE stock_snapshots SET confirmed=1, confirmed_by=? WHERE snapshot_id=?", (confirmed_by, dup["snapshot_id"]))
            conn.commit()
            return {"status": "unchanged", "taken_at": dup["taken_at"], "message": "That stock report was already loaded. It is now marked as confirmed."}
        return {"status": "unchanged", "taken_at": dup["taken_at"], "message": "That stock report is already loaded. Nothing changed."}
    taken_at = taken_at or stock_taken_at(file_name)
    items = [r for r in rows if r.get("Name") and not EXCLUDE_NAME.match(r["Name"])]
    if not items:
        raise StoreError("That stock report has no products in it.")
    replaced = conn.execute("SELECT snapshot_id, import_id FROM stock_snapshots WHERE taken_at = ?", (taken_at,)).fetchone()
    note_txt = note
    if replaced:
        conn.execute("UPDATE imports SET superseded=1 WHERE import_id=?", (replaced["import_id"],))
        conn.execute("DELETE FROM stock_snapshots WHERE snapshot_id=?", (replaced["snapshot_id"],))
        note_txt = (note + " " if note else "") + f"replaced a different report taken at the same time ({taken_at})"
    imp = _log(conn, "stock", file_name, digest, source, len(items), int(sum(num(r.get("TotalStock")) for r in items)), note_txt)
    sid = conn.execute("INSERT INTO stock_snapshots(taken_at, import_id, confirmed, confirmed_by, note) VALUES (?,?,?,?,?)",
                       (taken_at, imp, int(confirmed), confirmed_by or None, note or None)).lastrowid
    negatives = 0
    for r in items:
        pid = _product_id(conn, r["Name"], barcode=r.get("Barcode"), brand=r.get("Brand"), supplier=r.get("Supplier"),
                          order_code=r.get("OrderCode"), category=r.get("CategoryName"))
        ts = num(r.get("TotalStock"))
        negatives += ts < 0
        conn.execute("INSERT OR REPLACE INTO stock_levels VALUES (?,?,?,?,?,?)",
                     (sid, pid, ts, num(r.get("MeasuredOnOrder")), num(r.get("MeasuredCostPrice")), num(r.get("MeasuredSalePrice"), None)))
    cfg = _config(root)
    if cfg:
        classify_all(conn, cfg)
    conn.commit()
    msg = f"Loaded the stock report from {taken_at.replace('T', ' ')}: {len(items)} products."
    if negatives:
        msg += f" {negatives} product(s) show negative stock in Epos Now (see Data & sync)."
    msg += " It is marked as confirmed." if confirmed else " It is not marked as confirmed by a stocktake."
    out = {"status": "added", "taken_at": taken_at, "products": len(items), "negatives": negatives, "message": msg}
    if backup:
        out["backup"] = run_backup(conn, root)
    return out


def confirm_snapshot(conn, snapshot_id: int, by: str = "", on: bool = True) -> None:
    conn.execute("UPDATE stock_snapshots SET confirmed=?, confirmed_by=? WHERE snapshot_id=?", (int(on), by or None, snapshot_id))
    conn.commit()


# --------------------------------------------------------------------------- holdings
def set_holdings(conn, rows: list[tuple[str, int]], location: str = "Held outside Epos Now", as_of: str | None = None,
                 root: Path = ROOT, backup: bool = True) -> dict:
    """Replace the list of stock held outside Epos Now."""
    as_of = as_of or date.today().isoformat()
    conn.execute("DELETE FROM holdings")
    n = 0
    for name, qty in rows:
        name = str(name).strip()
        if not name:
            continue
        pid = _product_id(conn, name)
        conn.execute("INSERT OR REPLACE INTO holdings VALUES (?,?,?,?,NULL)", (pid, location, int(qty), as_of))
        n += 1
    _log(conn, "holding", "", "", "manual", n, sum(int(q) for _, q in rows if str(_).strip()), "")
    cfg = _config(root)
    if cfg:
        classify_all(conn, cfg)
    conn.commit()
    out = {"status": "saved", "rows": n, "message": f"Saved {n} line(s) of stock held outside Epos Now."}
    if backup:
        out["backup"] = run_backup(conn, root)
    return out


def get_holdings(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT p.name AS Name, h.qty AS Qty, h.as_of AS as_of, h.location FROM holdings h JOIN products p USING(product_id) ORDER BY p.name")]


# --------------------------------------------------------------------------- enrolments and notes
YEAR_LEVELS = ["Prep", "1", "2", "3", "4", "5", "6"]


def set_enrolments(conn, rows: list[dict], source: str = "typed in on the dashboard", root: Path = ROOT, backup: bool = True) -> dict:
    """Replace the enrolment table. rows: {year, year_level, students}. Blank students are skipped.
    A year may have only a Total (the breakdown isn't always available)."""
    clean = []
    for r in rows:
        try:
            y, lvl, n = int(r["year"]), str(r["year_level"]).strip(), r.get("students")
        except (KeyError, ValueError, TypeError):
            continue
        if n in (None, "", "N/A") or not lvl:
            continue
        n = int(float(n))
        if n < 0 or not 1990 <= y <= 2100:
            raise StoreError(f"Enrolment of {n} for {y} {lvl} doesn't look right.")
        clean.append((y, "Prep" if lvl.lower() in ("p", "prep", "prep year") else lvl.replace("Year ", ""), n, r.get("source") or source, r.get("note")))
    conn.execute("DELETE FROM enrolments")
    conn.executemany("INSERT OR REPLACE INTO enrolments VALUES (?,?,?,?,?)", clean)
    _log(conn, "holding", "", "", "enrolments", len(clean), 0, "enrolments saved")
    conn.commit()
    out = {"status": "saved", "rows": len(clean), "message": f"Saved {len(clean)} enrolment figure(s)."}
    if backup:
        out["backup"] = run_backup(conn, root)
    return out


def enrolment_totals(conn) -> dict[int, int]:
    """Total students per year: the 'Total' row if there is one, otherwise the sum of the year levels."""
    tot: dict[int, int] = {}
    for r in conn.execute("SELECT year, year_level, students FROM enrolments"):
        if r["year_level"] == "Total":
            tot[r["year"]] = r["students"]
    sums: dict[int, int] = {}
    for r in conn.execute("SELECT year, SUM(students) AS n FROM enrolments WHERE year_level <> 'Total' GROUP BY year"):
        sums[r["year"]] = r["n"]
    return {y: tot.get(y, sums.get(y)) for y in sorted(set(tot) | set(sums))}


def get_enrolment_grid(conn) -> list[dict]:
    """One row per year level, one column per year - the shape of a typical published table."""
    data = {(r["year_level"], r["year"]): r["students"] for r in conn.execute("SELECT year, year_level, students FROM enrolments")}
    years = sorted({y for _, y in data})
    out = []
    for lvl in [*YEAR_LEVELS, "Total"]:
        row = {"Year level": lvl}
        for y in years:
            v = data.get((lvl, y))
            if v is None and lvl == "Total":
                v = enrolment_totals(conn).get(y)
            row[str(y)] = v
        out.append(row)
    return out


def add_annotation(conn, year: int, term: int | None, note: str, root: Path = ROOT, backup: bool = True) -> None:
    if not note.strip():
        raise StoreError("Write the note first.")
    conn.execute("INSERT INTO annotations(year, term, note, created_at) VALUES (?,?,?,?)", (int(year), int(term) if term else None, note.strip(), now()))
    conn.commit()
    if backup:
        run_backup(conn, root)


def delete_annotation(conn, annotation_id: int) -> None:
    conn.execute("DELETE FROM annotations WHERE annotation_id=?", (annotation_id,))
    conn.commit()


def get_annotations(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT annotation_id, year, term, note, created_at FROM annotations ORDER BY year, COALESCE(term,0), annotation_id")]


# --------------------------------------------------------------------------- reading it back
def load_inputs(conn, clamp_negative: bool = True) -> dict:
    """Everything forecast.compute() needs, plus what the dashboard shows about the data."""
    quarters: dict = {}
    for r in conn.execute("SELECT s.year, s.term, p.name, s.qty FROM sales s JOIN products p USING(product_id)"):
        quarters.setdefault((r["year"], r["term"]), {})[r["name"]] = r["qty"]
    terms = {}
    for r in conn.execute("SELECT t.year, t.term, t.source, t.units, t.period_start, t.period_end, i.file_name, i.imported_at "
                          "FROM terms t LEFT JOIN imports i USING(import_id) ORDER BY t.year, t.term"):
        terms[(r["year"], r["term"])] = dict(r)
        quarters.setdefault((r["year"], r["term"]), {})
    snaps = []
    for s in conn.execute("SELECT * FROM stock_snapshots ORDER BY taken_at"):
        lv = {r["name"]: r["total_stock"] for r in conn.execute(
            "SELECT p.name, l.total_stock FROM stock_levels l JOIN products p USING(product_id) WHERE l.snapshot_id=?", (s["snapshot_id"],))}
        snaps.append({"snapshot_id": s["snapshot_id"], "taken_at": s["taken_at"], "confirmed": bool(s["confirmed"]),
                      "confirmed_by": s["confirmed_by"], "levels": lv})
    stock, on_order, costs, negatives, latest = {}, {}, {}, {}, None
    if snaps:
        latest = snaps[-1]
        for r in conn.execute("SELECT p.name, l.total_stock, l.on_order, l.cost_price FROM stock_levels l "
                              "JOIN products p USING(product_id) WHERE l.snapshot_id=?", (latest["snapshot_id"],)):
            raw = r["total_stock"] or 0
            if raw < 0:
                negatives[r["name"]] = int(raw)
            stock[r["name"]] = int(max(0, raw) if clamp_negative else raw)
            on_order[r["name"]] = int(r["on_order"] or 0)
            costs[r["name"]] = r["cost_price"] or 0.0
    holding = {r["Name"]: r["Qty"] for r in get_holdings(conn)}
    return {"enrolments": enrolment_totals(conn), "enrolment_grid": get_enrolment_grid(conn), "annotations": get_annotations(conn),
            "quarters": quarters, "terms": terms, "snapshots": snaps, "latest": latest, "stock": stock, "on_order": on_order,
            "costs": costs, "negatives": negatives, "holding": holding}


def status(conn) -> dict:
    q = lambda sql: conn.execute(sql).fetchone()[0]
    return {"products": q("SELECT COUNT(*) FROM products"), "terms": q("SELECT COUNT(*) FROM terms"),
            "sales_rows": q("SELECT COUNT(*) FROM sales"), "units": q("SELECT COALESCE(SUM(units),0) FROM terms"),
            "snapshots": q("SELECT COUNT(*) FROM stock_snapshots"), "enrolment_rows": q("SELECT COUNT(*) FROM enrolments"),
            "annotations": q("SELECT COUNT(*) FROM annotations"), "imports": q("SELECT COUNT(*) FROM imports"),
            "first_term": conn.execute("SELECT MIN(year*10+term) FROM terms").fetchone()[0],
            "last_term": conn.execute("SELECT MAX(year*10+term) FROM terms").fetchone()[0]}


# --------------------------------------------------------------------------- migration from the CSV folders
def ingest_folders(conn, root: Path = ROOT, backup: bool = False) -> list[str]:
    """Load any CSVs in data/sales, data/stock and holding.csv that the database doesn't have yet.
    Safe to run any time: identical files are recognised and skipped."""
    msgs = []
    sales_dir, stock_dir = root / "data" / "sales", root / "data" / "stock"
    # real exports first, so a seeded file never wins over a real one for the same term
    files = sorted(sales_dir.glob("*.csv"), key=lambda p: (1 if "seed" in p.stem.lower() else 0, p.name)) if sales_dir.exists() else []
    for p in files:
        m = f.QFILE.search(p.stem)
        if not m:
            msgs.append(f"Ignored {p.name}: no year/quarter in the name")
            continue
        src = "seeded" if "seed" in p.stem.lower() else "epos_export"
        try:
            r = import_sales(conn, p.read_bytes(), int(m.group(1)), int(m.group(2)), source=src, file_name=p.name, root=root, backup=False)
            if r["status"] in ("added", "replaced"):
                msgs.append(r["message"])
        except StoreError as e:
            msgs.append(f"{p.name}: {e}")
    if stock_dir.exists():
        for p in sorted(stock_dir.glob("*.csv"), key=f.stock_file_key):
            try:
                r = import_stock(conn, p.read_bytes(), file_name=p.name, root=root, backup=False)
                if r["status"] == "added":
                    msgs.append(r["message"])
            except StoreError as e:
                msgs.append(f"{p.name}: {e}")
    hp = root / "data" / "holding.csv"
    if hp.exists() and conn.execute("SELECT COUNT(*) FROM holdings").fetchone()[0] == 0:
        rows = [(r["Name"], int(num(r.get("Qty")))) for r in parse_csv(hp.read_bytes()) if r.get("Name")]
        if rows:
            set_holdings(conn, rows, as_of=date.fromtimestamp(hp.stat().st_mtime).isoformat(), root=root, backup=False)
            msgs.append(f"Loaded {len(rows)} line(s) of stock held outside Epos Now from holding.csv.")
    ep = root / "data" / "enrolments.csv"
    if ep.exists() and conn.execute("SELECT COUNT(*) FROM enrolments").fetchone()[0] == 0:
        rows = [{"year": r["Year"], "year_level": r["YearLevel"], "students": r["Students"], "source": r.get("Source"), "note": r.get("Note")}
                for r in parse_csv(ep.read_bytes())]
        res = set_enrolments(conn, rows, source="data/enrolments.csv", root=root, backup=False)
        msgs.append(f"Loaded enrolments from enrolments.csv ({res['rows']} figures).")
    if backup:
        run_backup(conn, root)
    return msgs


def ensure_ready(root: Path = ROOT) -> sqlite3.Connection:
    """Open the database; on first use, build it from whatever CSVs are already in the data folders."""
    path = root / "data" / "stock.db"
    conn = open_db(path)
    if is_empty(conn):
        ingest_folders(conn, root)
    return conn


# --------------------------------------------------------------------------- export / restore
EXPORT_FILES = ["sales_history.csv", "stock_levels.csv", "stock_snapshots.csv", "holdings.csv", "products.csv", "terms.csv", "imports.csv",
                "enrolments.csv", "annotations.csv"]

EXPORT_README = """Stock planner - full data export
=============================================
Written by the stock planner. Keep this folder on the shared folder (SharePoint, OneDrive, Dropbox...) as the off-computer copy.

sales_history.csv     THE MAIN FILE. Every product's units and dollars for every term loaded, one row each.
stock_levels.csv      Every stock report loaded: stock on hand, on order and cost price per product.
stock_snapshots.csv   One row per stock report: when it was run and whether the stock manager confirmed it.
holdings.csv          Stock held outside Epos Now (for example by a supplier).
enrolments.csv        Students per year level per year (August census), as typed in on the dashboard.
annotations.csv       Notes about one-off events that explain a number.
products.csv          Each product with its supplier, category, product line and size.
terms.csv             Each term loaded: dates, where it came from, total units.
imports.csv           Audit log: every file loaded, when, and its checksum.

To rebuild the database on a new computer:  python store.py restore <this folder's zip>   (or the folder itself)
"""


def _table_csv(conn, sql: str, headers: list[str] | None = None) -> str:
    cur = conn.execute(sql)
    cols = headers or [d[0] for d in cur.description]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in cur:
        w.writerow(["" if v is None else v for v in r])
    return buf.getvalue()


def export_texts(conn) -> dict[str, str]:
    return {
        "sales_history.csv": _table_csv(conn, """
            SELECT t.year AS year, t.term AS term, t.year || 'Q' || t.term AS term_label, t.period_start, t.period_end,
                   p.name AS product, COALESCE(p.line,'') AS product_line, COALESCE(p.size_label,'') AS size,
                   COALESCE(p.category,'') AS category, COALESCE(p.supplier,'') AS supplier,
                   s.qty AS qty, s.value_inc_vat, s.value_exc_vat, s.discount, s.total_cost,
                   t.source AS source
            FROM sales s JOIN terms t USING(year, term) JOIN products p USING(product_id)
            ORDER BY t.year, t.term, p.name"""),
        "stock_levels.csv": _table_csv(conn, """
            SELECT s.taken_at, p.name AS product, COALESCE(p.line,'') AS product_line, COALESCE(p.size_label,'') AS size,
                   l.total_stock, l.on_order, l.cost_price, l.sale_price, s.snapshot_id
            FROM stock_levels l JOIN stock_snapshots s USING(snapshot_id) JOIN products p USING(product_id)
            ORDER BY s.taken_at, p.name"""),
        "stock_snapshots.csv": _table_csv(conn, "SELECT snapshot_id, taken_at, import_id, confirmed, confirmed_by, note FROM stock_snapshots ORDER BY taken_at"),
        "holdings.csv": _table_csv(conn, "SELECT p.name AS product, h.location, h.qty, h.as_of, h.note FROM holdings h JOIN products p USING(product_id) ORDER BY p.name"),
        "products.csv": _table_csv(conn, "SELECT product_id, name, epos_id, barcode, brand, supplier, order_code, category, line, size_raw, size_label FROM products ORDER BY name"),
        "terms.csv": _table_csv(conn, "SELECT year, term, period_start, period_end, source, import_id, units FROM terms ORDER BY year, term"),
        "enrolments.csv": _table_csv(conn, "SELECT year, year_level, students, source, note FROM enrolments ORDER BY year, CASE year_level WHEN 'Prep' THEN 0 WHEN 'Total' THEN 9 ELSE CAST(year_level AS INTEGER) END"),
        "annotations.csv": _table_csv(conn, "SELECT annotation_id, year, term, note, created_at FROM annotations ORDER BY annotation_id"),
        "imports.csv": _table_csv(conn, "SELECT import_id, kind, file_name, sha256, source, imported_at, rows, units, note, superseded FROM imports ORDER BY import_id"),
    }


def export_to_folder(conn, folder: Path) -> list[Path]:
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for name, text in export_texts(conn).items():
        p = folder / name
        p.write_text(text, encoding="utf-8", newline="")
        out.append(p)
    (folder / "README.txt").write_text(EXPORT_README, encoding="utf-8")
    return out


def export_zip_bytes(conn) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in export_texts(conn).items():
            z.writestr(name, text)
        z.writestr("README.txt", EXPORT_README)
    return buf.getvalue()


def run_backup(conn, root: Path = ROOT) -> str:
    """After every load: write the full CSV export to backup_dir (set it to a synced shared folder (SharePoint, OneDrive...))."""
    cfg = _config(root)
    target = (cfg or {}).get("settings", {}).get("backup_dir", "") if cfg else ""
    target = Path(target).expanduser() if target else root / "output" / "backup"
    try:
        export_to_folder(conn, target)
        return f"Full CSV export written to {target}"
    except OSError as e:
        return f"WARNING: could not write the backup export to {target} ({e}). Use Download backup instead."


def _has(read, name: str) -> bool:
    try:
        read(name)
        return True
    except (KeyError, FileNotFoundError):
        return False


def restore(conn, source: Path) -> dict:
    """Rebuild an EMPTY database from an export (a zip, or the folder of CSVs)."""
    if not is_empty(conn) or conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]:
        raise StoreError("Restore only works on an empty database. Move or rename data/stock.db first.")
    source = Path(source)
    if source.is_dir():
        read = lambda n: list(csv.DictReader(open(source / n, newline="", encoding="utf-8")))
    else:
        z = zipfile.ZipFile(source)
        read = lambda n: list(csv.DictReader(io.TextIOWrapper(z.open(n), encoding="utf-8", newline="")))
    n = lambda v: None if v in ("", None) else v
    try:
        imports, products, terms = read("imports.csv"), read("products.csv"), read("terms.csv")
        snaps, levels, hold, sales = read("stock_snapshots.csv"), read("stock_levels.csv"), read("holdings.csv"), read("sales_history.csv")
    except (KeyError, FileNotFoundError) as e:
        raise StoreError(f"That export is missing a file ({e}). It needs all of: {', '.join(EXPORT_FILES)}") from None
    with conn:
        for r in imports:
            conn.execute("INSERT INTO imports VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (int(r["import_id"]), r["kind"], r["file_name"], r["sha256"], r["source"], r["imported_at"],
                          n(r["rows"]), n(r["units"]), r["note"], int(r["superseded"] or 0)))
        for r in products:
            conn.execute("INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (int(r["product_id"]), r["name"], n(r["epos_id"]), n(r["barcode"]), n(r["brand"]), n(r["supplier"]),
                          n(r["order_code"]), n(r["category"]), n(r["line"]), n(r["size_raw"]), n(r["size_label"])))
        for r in terms:
            conn.execute("INSERT INTO terms VALUES (?,?,?,?,?,?,?)", (int(r["year"]), int(r["term"]), r["period_start"], r["period_end"],
                                                                      r["source"], n(r["import_id"]), int(r["units"])))
        pid = {r["name"]: int(r["product_id"]) for r in products}
        for r in sales:
            conn.execute("INSERT INTO sales VALUES (?,?,?,?,?,?,?,?)",
                         (int(r["year"]), int(r["term"]), pid[r["product"]], int(float(r["qty"])), n(r["value_inc_vat"]),
                          n(r["value_exc_vat"]), n(r["discount"]), n(r["total_cost"])))
        for r in snaps:
            conn.execute("INSERT INTO stock_snapshots VALUES (?,?,?,?,?,?)", (int(r["snapshot_id"]), r["taken_at"], n(r["import_id"]),
                                                                              int(r["confirmed"] or 0), n(r["confirmed_by"]), n(r["note"])))
        for r in levels:
            conn.execute("INSERT INTO stock_levels VALUES (?,?,?,?,?,?)", (int(r["snapshot_id"]), pid[r["product"]], n(r["total_stock"]),
                                                                           n(r["on_order"]), n(r["cost_price"]), n(r["sale_price"])))
        for r in hold:
            conn.execute("INSERT INTO holdings VALUES (?,?,?,?,?)", (pid[r["product"]], r["location"], int(float(r["qty"])), n(r["as_of"]), n(r["note"])))
        for r in (read("enrolments.csv") if _has(read, "enrolments.csv") else []):
            conn.execute("INSERT INTO enrolments VALUES (?,?,?,?,?)", (int(r["year"]), r["year_level"], int(r["students"]), n(r["source"]), n(r["note"])))
        for r in (read("annotations.csv") if _has(read, "annotations.csv") else []):
            conn.execute("INSERT INTO annotations VALUES (?,?,?,?,?)", (int(r["annotation_id"]), int(r["year"]), n(r["term"]), r["note"], r["created_at"]))
        _log(conn, "restore", str(source.name), "", "export", len(sales), 0, "database rebuilt from an export")
    return status(conn)


# --------------------------------------------------------------------------- command line
def main(argv=None) -> None:
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] in ("-h", "--help", "help"):
        print(__doc__)
        return
    cmd, rest = a[0], a[1:]
    try:
        if cmd == "restore":
            if len(rest) != 1:
                raise StoreError("Usage: python store.py restore EXPORT.zip")
            conn = open_db()
            print("Restored:", restore(conn, Path(rest[0])))
            return
        conn = ensure_ready()
        if cmd == "status":
            for k, v in status(conn).items():
                print(f"{k:12} {v}")
        elif cmd == "ingest":
            msgs = ingest_folders(conn, backup=True)
            print("\n".join(msgs) if msgs else "Nothing new to load.")
        elif cmd == "import-sales" and len(rest) == 2:
            m = f.QFILE.search(rest[1])
            if not m:
                raise StoreError("Say which term, e.g.  2027Q1")
            p = Path(rest[0])
            r = import_sales(conn, p.read_bytes(), int(m.group(1)), int(m.group(2)), file_name=p.name)
            print(r["message"], r.get("backup", ""), sep="\n")
        elif cmd == "import-stock" and rest:
            p = Path(rest[0])
            r = import_stock(conn, p.read_bytes(), file_name=p.name, confirmed="--confirmed" in rest)
            print(r["message"], r.get("backup", ""), sep="\n")
        elif cmd == "export":
            folder = Path(rest[0]) if rest else ROOT / "output" / "backup"
            files = export_to_folder(conn, folder)
            (folder / "stock_planner_data_export.zip").write_bytes(export_zip_bytes(conn))
            print(f"Wrote {len(files)} CSV files and stock_planner_data_export.zip to {folder}")
        else:
            print(__doc__)
    except StoreError as e:
        sys.exit(f"Problem: {e}")


if __name__ == "__main__":
    main()
