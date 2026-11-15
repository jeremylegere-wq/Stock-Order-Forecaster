#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Stock order forecaster
======================
Reads quarterly POS "Sales by Product" exports, the latest "Stock Levels" export and any
externally-held stock, and works out how many of each item to order.

Method (the classic order-up-to method):
  * Quarters are grouped into annual periods running Term 4 -> Term 3
    (2025Q4 + 2026Q1 + 2026Q2 + 2026Q3 = period "2025T4-2026T3").
  * Target            = highest annual period on record x (1 + buffer)
  * Stock at delivery = POS stock + stock already on order + externally held stock
                        - sales expected before the order arrives (only for lines with a delivery_quarter)
  * Suggested order   = MAX(0, target - stock at delivery)
  * Confidence        = n / (n + 1) for n complete years of history (distribution-free).

"Sales expected before the order arrives": a line with `delivery_quarter = 4` gets its order in Term 4.
Run after Term 1 (the main yearly order), Terms 2 and 3 are still to be sold out of today's stock, so their
expected sales (average + uncertainty of the same terms in past years) are deducted - this is the classic
"projected stock" at delivery. Run after Term 3, nothing is left to sell before delivery, so nothing is deducted.

Usage:
  python forecast.py --init     # create the folders (first time only)
  python forecast.py            # run the forecast
"""
from __future__ import annotations

import argparse
import csv
import math
import re
import shutil
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        sys.exit("Python 3.11+ is required (or run: pip install tomli).")

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
except ModuleNotFoundError:
    sys.exit("Missing dependency. Run: pip install -r requirements.txt")

ROOT = Path(__file__).resolve().parent
QFILE = re.compile(r"(\d{4})\s*[_-]?\s*Q([1-4])", re.I)
STOCKDATE = re.compile(r"(\d{4})[_-](\d{2})[_-](\d{2})(?:[_-](\d{4}))?")
ALPHA_SIZES = ["3XS", "2XS", "XXS", "XS", "S", "M", "L", "XL", "2XL", "XXL", "3XL"]
DETAIL = "Before-delivery detail"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def ceil_(x: float) -> int:
    """Excel-style ROUNDUP(x, 0) for positive numbers, immune to float noise
    (10 * 1.1 is 11.000000000000002 in Python but 11 in Excel)."""
    return math.ceil(round(x, 9))


def period_of(year: int, q: int) -> int:
    """Q1-Q3 belong to the same-numbered period; Q4 starts the next one."""
    return year if q <= 3 else year + 1


def period_label(p: int) -> str:
    return f"{p - 1}T4-{p}T3"


def qlabel(key: tuple[int, int]) -> str:
    return f"{key[0]}Q{key[1]}"


def size_key(label: str) -> tuple:
    """Sort sizes sensibly: letter sizes (XS < S < M ...) in order, then numeric sizes."""
    up = label.strip().upper()
    if up in ALPHA_SIZES:
        return (0, ALPHA_SIZES.index(up), up)
    m = re.match(r"\d+", up)
    return (1, int(m.group()), up) if m else (2, 0, up)


def window_after(last: tuple[int, int], delivery_q: int) -> list[tuple[int, int]]:
    """Quarters that fall after `last` and before the next quarter numbered `delivery_q`."""
    out, (y, q) = [], last
    for _ in range(4):
        q += 1
        if q == 5:
            y, q = y + 1, 1
        if q == delivery_q:
            break
        out.append((y, q))
    return out


def quarter_end(key: tuple[int, int]) -> date:
    y, q = key
    return date(y, 3 * q, (31, 30, 30, 31)[q - 1])


def auto_partial(quarters, warnings: list[str], today: date | None = None) -> set[str]:
    """A term that hasn't finished yet can't be a full term, whatever the file says."""
    today = today or date.today()
    out = set()
    for k in quarters:
        if today <= quarter_end(k):
            out.add(qlabel(k))
            warnings.append(f"{qlabel(k)} is still in progress (ends {quarter_end(k):%d %b %Y}) - treated as a partial term")
    return out


def terms_text(window: list[tuple[int, int]]) -> str:
    return "+".join(f"T{q}" for _, q in window) if window else "none"


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"Config not found: {path}")
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cfg.setdefault("settings", {})
    if not cfg.get("lines"):
        sys.exit(f"{path}: no [[lines]] defined - nothing to forecast.")
    for ln in cfg["lines"]:
        if "name" not in ln or "match" not in ln:
            sys.exit(f"{path}: every [[lines]] block needs a 'name' and a 'match' pattern.")
        dq = ln.get("delivery_quarter")
        if dq is not None and dq not in (1, 2, 3, 4):
            sys.exit(f"{path}: line '{ln['name']}': delivery_quarter must be 1, 2, 3 or 4.")
    return cfg


def read_rows(path: Path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            yield {(k or "").strip(): (v or "").strip() for k, v in row.items()}


def to_int(v: str) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return 0


def load_sales(path: Path) -> dict[str, int]:
    totals: dict[str, int] = defaultdict(int)
    for row in read_rows(path):
        name = row.get("Name", "")
        if name:
            totals[name] += to_int(row.get("Qty", "0"))
    return dict(totals)


def discover_quarters(sales_dir: Path, warnings: list[str]):
    """One file per quarter. A real POS export beats a 'seeded' file; otherwise the newer file wins."""
    found: dict[tuple[int, int], Path] = {}
    rank = lambda p: (0 if "seed" in p.stem.lower() else 1, p.stat().st_mtime)
    for p in sorted(sales_dir.glob("*.csv"), key=rank):
        m = QFILE.search(p.stem)
        if not m:
            warnings.append(f"Ignored {p.name}: filename has no year/quarter (expected e.g. 2026Q3.csv)")
            continue
        key = (int(m.group(1)), int(m.group(2)))
        if key in found:
            warnings.append(f"{qlabel(key)}: found both {found[key].name} and {p.name}; using {p.name}")
        found[key] = p
    files = dict(sorted(found.items()))
    return {k: load_sales(p) for k, p in files.items()}, {k: p.name for k, p in files.items()}


def stock_file_key(p: Path):
    m = STOCKDATE.search(p.stem)
    return (1, m.group(1) + m.group(2) + m.group(3) + (m.group(4) or "0000")) if m else (0, f"{p.stat().st_mtime:020.3f}")


def load_stock(stock_dir: Path):
    """Newest stock report: by the date in its filename (StockReport_2026_10_05_0530.csv), else by file time."""
    files = sorted(stock_dir.glob("*.csv"), key=stock_file_key)
    if not files:
        sys.exit(f"No stock report found in {stock_dir} (export Reporting > Stock > Stock Levels).")
    latest = files[-1]
    stock, on_order = {}, {}
    for row in read_rows(latest):
        if row.get("Name"):
            stock[row["Name"]] = to_int(row.get("TotalStock", "0"))
            on_order[row["Name"]] = to_int(row.get("MeasuredOnOrder", "0"))
    return stock, on_order, latest.name


def load_holding(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    return {r["Name"]: to_int(r.get("Qty", "0")) for r in read_rows(path) if r.get("Name")}


# --------------------------------------------------------------------------- #
# Forecast
# --------------------------------------------------------------------------- #
def compute(cfg, quarters, partial_q, stock, on_order, holding, warnings):
    settings = cfg["settings"]
    buffer_pct = settings.get("buffer_pct", 10)
    mult = 1 + buffer_pct / 100
    use_on_order = bool(settings.get("include_on_order", True))
    lines = cfg["lines"]
    patterns = [(ln, re.compile(ln["match"])) for ln in lines]

    # ---- which items are we forecasting? ----
    names = set(stock) | set(holding)
    for totals in quarters.values():
        names |= set(totals)
    excl = [re.compile(p, re.I) for p in settings.get("exclude", [r"^total:?$"])]
    names = {n for n in names if not any(x.search(n) for x in excl)}
    items: dict[str, dict] = {}
    for name in names:
        for ln, rx in patterns:
            m = rx.match(name)
            if m:
                raw = m.group(1) if m.groups() else name
                items[name] = {"name": name, "line": ln["name"], "raw": raw,
                               "size": ln.get("size_labels", {}).get(raw, raw)}
                break
    if not items:
        sys.exit("No products matched the patterns in config.toml - check the [[lines]] 'match' settings.")
    for name in holding:
        if name not in items:
            warnings.append(f"holding.csv: '{name}' doesn't match any product line - ignored")
    labels = {qlabel(k) for k in quarters}
    for q in sorted(partial_q):
        if q not in labels:
            warnings.append(f"partial_quarters lists {q} but no file for that quarter was found")

    # ---- annual periods (T4 -> T3) ----
    periods = sorted({period_of(*k) for k in quarters})
    pinfo = {}
    for p in periods:
        wanted = [(p - 1, 4), (p, 1), (p, 2), (p, 3)]
        present = [k for k in wanted if k in quarters]
        missing = [qlabel(k) for k in wanted if k not in quarters]
        partial = bool(missing) or any(qlabel(k) in partial_q for k in present)
        pinfo[p] = {"label": period_label(p), "present": present, "missing": missing, "complete": not partial}
    qs = sorted(quarters)
    for p in periods:
        if pinfo[p]["missing"] and p not in (period_of(*qs[-1]), period_of(*qs[0])):
            warnings.append(f"Period {period_label(p)} is missing {', '.join(pinfo[p]['missing'])} - treated as partial")

    for it in items.values():
        it["vals"] = {p: sum(quarters[k].get(it["name"], 0) for k in pinfo[p]["present"]) for p in periods}

    # ---- which terms are still to be sold before each line's order arrives? ----
    complete_q = [k for k in qs if qlabel(k) not in partial_q]
    last = max(complete_q) if complete_q else None

    line_info = {}
    for ln in lines:
        its = [i for i in items.values() if i["line"] == ln["name"]]
        # years before the line's first recorded sale are "no data", not zero sales
        first = next((p for p in periods if any(i["vals"][p] for i in its)), None)
        for i in its:
            for p in periods:
                if first is None or p < first:
                    i["vals"][p] = None
        n = sum(1 for p in periods if pinfo[p]["complete"] and first is not None and p >= first)
        dq = ln.get("delivery_quarter")
        window = window_after(last, dq) if (dq and last) else []
        line_info[ln["name"]] = {"first": first, "n": n, "confidence": n / (n + 1) if n else 0.0,
                                 "delivery_quarter": dq, "window": window, "instances": 0}
        for i in its:
            i["window"] = window
            i["inst"] = []            # (label, units) for the same terms in earlier years, newest first
            if window:
                for back in range(1, 40):
                    past = [(y - back, q) for y, q in window]
                    if any(k not in quarters or qlabel(k) in partial_q for k in past):
                        if all(k not in quarters for k in past):
                            break
                        continue
                    if any(i["vals"].get(period_of(*k)) is None for k in past):
                        continue
                    label = "+".join(qlabel(k) for k in past)
                    i["inst"].append((label, sum(quarters[k].get(i["name"], 0) for k in past)))
                line_info[ln["name"]]["instances"] = max(line_info[ln["name"]]["instances"], len(i["inst"]))
        if window and its and not line_info[ln["name"]]["instances"]:
            warnings.append(f"{ln['name']}: no past years cover {terms_text(window)}, so nothing was deducted before delivery")

    for it in items.values():
        comp = [it["vals"][p] for p in periods if pinfo[p]["complete"] and it["vals"][p] is not None]
        every = [it["vals"][p] for p in periods if it["vals"][p] is not None]
        it["avg"] = ceil_(sum(comp) / len(comp)) if comp else None
        it["unc"] = ceil_((max(comp) - min(comp)) / 2) if comp else None
        it["target"] = ceil_(max(every) * mult) if every else None
        it["stock"] = stock.get(it["name"], 0)
        it["on_order"] = on_order.get(it["name"], 0)
        it["holding"] = holding.get(it["name"], 0)
        v = [u for _, u in it["inst"]]
        it["expected"] = (ceil_(sum(v) / len(v)) + ceil_((max(v) - min(v)) / 2)) if v else 0
        it["available"] = it["stock"] + (it["on_order"] if use_on_order else 0) + it["holding"] - it["expected"]
        it["order"] = max(0, it["target"] - it["available"]) if it["target"] is not None else None

    order = {ln["name"]: i for i, ln in enumerate(lines)}
    rows = sorted(items.values(), key=lambda i: (order[i["line"]], size_key(i["raw"])))
    meta = {"buffer_pct": buffer_pct, "use_on_order": use_on_order, "last_complete": last}
    return rows, periods, pinfo, line_info, meta


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def write_excel(path, rows, periods, pinfo, line_info, meta):
    buffer_pct, use_on_order = meta["buffer_pct"], meta["use_on_order"]
    wb = Workbook()
    ws = wb.active
    ws.title = "Order Forecast"

    hfill = PatternFill("solid", fgColor="FF1F4E78")
    hfont = Font(bold=True, color="FFFFFFFF")
    blue = Font(name="Arial", size=10, color="FF0000FF")
    green = Font(name="Calibri", size=11, color="FF008000")
    black = Font(name="Calibri", size=11)
    yellow = PatternFill("solid", fgColor="FFFFFF00")
    grey = PatternFill("solid", fgColor="FFEDEDED")
    thin = Side(style="thin", color="FFBFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    centre = Alignment(wrap_text=True, vertical="center", horizontal="center")
    L = get_column_letter

    def head(sheet, row, col, text):
        c = sheet.cell(row=row, column=col, value=text)
        c.font, c.fill, c.alignment, c.border = hfont, hfill, centre, border

    # ---- detail sheet: the same terms in earlier years, for lines that deduct pre-delivery sales ----
    detail_rows = [it for it in rows if it["window"]]
    drow = {}
    if detail_rows:
        ds = wb.create_sheet(DETAIL)
        k = max(len(it["inst"]) for it in detail_rows) or 1
        ds["A1"] = "Sales expected before the order arrives"
        ds["A1"].font = Font(bold=True, size=14, color="FF1F4E78")
        ds["A2"] = ("For each item: what the terms still to be sold before delivery sold in earlier years (newest first). "
                    "Expected = ROUNDUP(average) + ROUNDUP((max - min) / 2).")
        ds["A2"].font = Font(size=9, italic=True, color="FF808080")
        for c, t in enumerate(["Product", "Size", "Terms deducted"], start=1):
            head(ds, 4, c, t)
        for j in range(k):
            head(ds, 4, 4 + j, f"{j + 1} yr{'s' if j else ''} earlier")
        head(ds, 4, 4 + k, "Expected\n(avg + uncertainty)")
        ds.row_dimensions[4].height = 32
        r = 5
        for it in detail_rows:
            ds.cell(row=r, column=1, value=it["line"]).border = border
            ds.cell(row=r, column=2, value=it["size"]).border = border
            ds.cell(row=r, column=3, value=terms_text(it["window"])).border = border
            for j in range(k):
                c = ds.cell(row=r, column=4 + j, value=it["inst"][j][1] if j < len(it["inst"]) else None)
                c.font, c.border = blue, border
            rng = f"{L(4)}{r}:{L(3 + k)}{r}"
            c = ds.cell(row=r, column=4 + k,
                        value=f"=IF(COUNT({rng})=0,0,ROUNDUP(AVERAGE({rng}),0)+ROUNDUP((MAX({rng})-MIN({rng}))/2,0))")
            c.font, c.border, c.fill = black, border, yellow
            drow[it["name"]] = f"'{DETAIL}'!{L(4 + k)}{r}"
            r += 1
        ds.column_dimensions["A"].width, ds.column_dimensions["B"].width, ds.column_dimensions["C"].width = 26, 16, 15
        for c in range(4, 5 + k):
            ds.column_dimensions[L(c)].width = 13
        ds.freeze_panes = "D5"

    # ---- main sheet ----
    ws["A1"] = "Stock Order Forecast"
    ws["A1"].font = Font(bold=True, size=14, color="FF1F4E78")
    ws["A2"] = (f"Generated {meta['when']} | stock report: {meta['stock_file']} | target = highest year on record "
                f"+ {buffer_pct}% | order = MAX(0, target - stock at delivery). Blue = data, black = formula, "
                "green = from another sheet, grey year columns = partial (incomplete) periods.")
    ws["A2"].font = Font(size=9, italic=True, color="FF808080")

    pcol = {p: 3 + i for i, p in enumerate(periods)}
    c0 = 3 + len(periods)
    c_avg, c_unc, c_au, c_max, c_stock, c_onord, c_hold, c_exp, c_avail, c_ord = range(c0, c0 + 10)
    heads = {1: "Product", 2: "Size", c_avg: "Avg\n(complete yrs)", c_unc: "Uncertainty\n(max-min)/2",
             c_au: "Avg +\nUncertainty", c_max: f"Target\n(max + {buffer_pct}%)", c_stock: "POS stock",
             c_onord: "On order" + ("" if use_on_order else "\n(not counted)"),
             c_hold: "Holding stock\n(external)", c_exp: "Expected sales\nbefore delivery",
             c_avail: "Stock at\ndelivery", c_ord: "Suggested\norder"}
    for p in periods:
        heads[pcol[p]] = pinfo[p]["label"] + ("" if pinfo[p]["complete"] else "\n(partial)")
    for c, text in heads.items():
        head(ws, 4, c, text)
    ws.row_dimensions[4].height = 45

    comp_p = [p for p in periods if pinfo[p]["complete"]]
    mult = round(1 + buffer_pct / 100, 6)
    r = 5
    for it in rows:
        ws.cell(row=r, column=1, value=it["line"]).border = border
        ws.cell(row=r, column=2, value=it["size"]).border = border
        for p in periods:
            c = ws.cell(row=r, column=pcol[p], value=it["vals"][p])
            c.font, c.border = blue, border
            if not pinfo[p]["complete"]:
                c.fill = grey
        rng = f"{L(pcol[periods[0]])}{r}:{L(pcol[periods[-1]])}{r}"
        comp = ",".join(f"{L(pcol[p])}{r}" for p in comp_p)
        if comp_p:
            ws.cell(row=r, column=c_avg, value=f'=IF(COUNT({comp})=0,"",ROUNDUP(AVERAGE({comp}),0))')
            ws.cell(row=r, column=c_unc, value=f'=IF(COUNT({comp})=0,"",ROUNDUP((MAX({comp})-MIN({comp}))/2,0))')
            ws.cell(row=r, column=c_au, value=f'=IF({L(c_avg)}{r}="","",{L(c_avg)}{r}+{L(c_unc)}{r})')
        ws.cell(row=r, column=c_max, value=f'=IF(COUNT({rng})=0,"",ROUNDUP(ROUND(MAX({rng})*{mult},9),0))')
        for c, val in ((c_stock, it["stock"]), (c_onord, it["on_order"]), (c_hold, it["holding"])):
            ws.cell(row=r, column=c, value=val).font = blue
        if it["name"] in drow:
            ws.cell(row=r, column=c_exp, value=f"={drow[it['name']]}").font = green
        else:
            ws.cell(row=r, column=c_exp, value=0).font = blue
        on = f"+{L(c_onord)}{r}" if use_on_order else ""
        ws.cell(row=r, column=c_avail, value=f"={L(c_stock)}{r}{on}+{L(c_hold)}{r}-{L(c_exp)}{r}")
        ws.cell(row=r, column=c_ord, value=f'=IF({L(c_max)}{r}="","",MAX(0,{L(c_max)}{r}-{L(c_avail)}{r}))')
        ws.cell(row=r, column=c_ord).fill = yellow
        for c in range(c_avg, c_ord + 1):
            ws.cell(row=r, column=c).border = border
        for c in (c_avg, c_unc, c_au, c_max, c_avail, c_ord):
            ws.cell(row=r, column=c).font = black
        r += 1
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 16
    for c in range(3, c_ord + 1):
        ws.column_dimensions[L(c)].width = 14
    ws.freeze_panes = "C5"

    # ---- run-info sheet ----
    info = wb.create_sheet("Run Info")
    info["A1"], info["B1"] = "Generated", meta["when"]
    info["A2"], info["B2"] = "Stock report", meta["stock_file"]
    info["A3"], info["B3"] = "Buffer", f"{buffer_pct}%"
    info["A4"], info["B4"] = "Stock already on order", "counted as stock" if use_on_order else "not counted"
    info["A5"], info["B5"] = "Latest complete quarter", qlabel(meta["last_complete"]) if meta["last_complete"] else "none"
    hdr = ["Product line", "Complete years (n)", "Base coverage n/(n+1)", "First period with sales",
           "Order arrives in", "Terms deducted before delivery"]
    for i, h in enumerate(hdr, start=1):
        info.cell(row=7, column=i, value=h).font = Font(bold=True)
    rr = 8
    for name, li in line_info.items():
        info.cell(row=rr, column=1, value=name)
        info.cell(row=rr, column=2, value=li["n"])
        info.cell(row=rr, column=3, value=li["confidence"]).number_format = "0.0%"
        info.cell(row=rr, column=4, value=period_label(li["first"]) if li["first"] else "no sales")
        info.cell(row=rr, column=5, value=f"Term {li['delivery_quarter']}" if li["delivery_quarter"] else "not set")
        info.cell(row=rr, column=6, value=(f"{terms_text(li['window'])} ({li['instances']} past years)"
                                           if li["window"] else "none"))
        rr += 1
    rr += 1
    info.cell(row=rr, column=1, value="Quarter files used").font = Font(bold=True)
    for q, fname in meta["files"].items():
        rr += 1
        info.cell(row=rr, column=1, value=qlabel(q))
        info.cell(row=rr, column=2, value=fname)
    if meta["warnings"]:
        rr += 2
        info.cell(row=rr, column=1, value="Warnings").font = Font(bold=True)
        for w in meta["warnings"]:
            rr += 1
            info.cell(row=rr, column=1, value=w)
    info.column_dimensions["A"].width = 30
    for c in "BCDEF":
        info.column_dimensions[c].width = 30
    wb.save(path)


def write_csv(path, rows, periods, pinfo):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Product", "Size"] + [period_label(p) + ("" if pinfo[p]["complete"] else " (partial)") for p in periods]
                   + ["Avg", "Uncertainty", "Target", "POS Stock", "On Order", "Holding",
                      "Expected sales before delivery", "Stock at delivery", "Suggested Order"])
        for it in rows:
            w.writerow([it["line"], it["size"]] + ["" if it["vals"][p] is None else it["vals"][p] for p in periods]
                       + [it["avg"], it["unc"], it["target"], it["stock"], it["on_order"], it["holding"],
                          it["expected"], it["available"], it["order"]])


def print_report(rows, line_info, pinfo, periods, meta, warnings, stock_file, only_orders=False):
    print(f"\nStock order forecast  (target = highest year on record + {meta['buffer_pct']}%)")
    print(f"Stock report: {stock_file}\n")
    w_line = min(26, max(len(it["line"]) for it in rows) + 2)
    w_size = min(20, max(len(it["size"]) for it in rows) + 2)
    print(f"{'Product':<{w_line}}{'Size':<{w_size}}{'Target':>7}{'Stock':>7}{'OnOrd':>7}{'Hold':>6}{'PreDel':>8}{'Order':>7}")
    print("-" * (w_line + w_size + 42))
    shown = 0
    for it in rows:
        if it["order"] is None:
            continue
        shown += 1
        if only_orders and not it["order"]:
            continue
        flag = "  <-- ORDER" if it["order"] else ""
        print(f"{it['line'][:w_line - 1]:<{w_line}}{it['size'][:w_size - 1]:<{w_size}}{it['target']:>7}{it['stock']:>7}"
              f"{it['on_order']:>7}{it['holding']:>6}{it['expected']:>8}{it['order']:>7}{flag}")
    n_order = sum(1 for it in rows if it["order"])
    print(f"\n{n_order} of {shown} items need an order, {sum(it['order'] or 0 for it in rows)} units in total.")
    print("OnOrd = already on order in POS" + (" (counted as stock)" if meta["use_on_order"] else " (NOT counted)")
          + "; Hold = held outside POS; PreDel = sales expected before the order arrives.\n")
    for name, li in line_info.items():
        ded = f"; deducts {terms_text(li['window'])} before Term {li['delivery_quarter']} delivery" if li["window"] else ""
        print(f"{name}: {li['n']} complete year(s) -> base coverage {li['confidence']:.0%}{ded}")
    partial = [pinfo[p]["label"] for p in periods if not pinfo[p]["complete"]]
    if partial:
        print(f"Partial period(s), used only as a floor on the maximum: {', '.join(partial)}")
    for w in warnings:
        print(f"WARNING: {w}")


# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="Stock order forecaster")
    ap.add_argument("--init", action="store_true", help="create the data/ and output/ folders and exit")
    ap.add_argument("--config", type=Path, default=ROOT / "config.toml")
    ap.add_argument("--partial", nargs="*", default=[], metavar="2026Q3",
                    help="extra quarters to treat as incomplete (adds to config partial_quarters)")
    ap.add_argument("--only-orders", action="store_true",
                    help="print only the items that need an order (the Excel/CSV output always lists everything)")
    args = ap.parse_args(argv)

    out_dir = ROOT / "output"
    if args.init:
        for d in (ROOT / "data" / "sales", ROOT / "data" / "stock", out_dir):
            d.mkdir(parents=True, exist_ok=True)
        import store
        store.ensure_ready(ROOT)
        print("Ready. Load data with the dashboard (python dashboard.py) or: python store.py import-sales FILE.csv 2027Q1")
        return
    out_dir.mkdir(exist_ok=True)

    import store
    cfg = load_config(args.config)
    warnings: list[str] = []
    partial_q = {q.upper() for q in cfg["settings"].get("partial_quarters", [])} | {q.upper() for q in args.partial}
    try:
        conn = store.ensure_ready(ROOT)
        inp = store.load_inputs(conn)
    except store.StoreError as e:
        sys.exit(f"Problem: {e}")
    quarters = inp["quarters"]
    if not quarters:
        sys.exit("No sales have been loaded. Use the dashboard or: python store.py import-sales FILE.csv 2027Q1")
    if not inp["latest"]:
        sys.exit("No stock report has been loaded. Use the dashboard or: python store.py import-stock FILE.csv")
    partial_q |= auto_partial(quarters, warnings)
    stock, on_order, holding = inp["stock"], inp["on_order"], inp["holding"]
    when = inp["latest"]["taken_at"].replace("T", " ")
    stock_file = f"database snapshot {when}" + ("" if inp["latest"]["confirmed"] else " (not confirmed by a stocktake)")
    files = {k: f"{t['source']}: {t['file_name'] or 'database'}" for k, t in inp["terms"].items()}
    for name, raw in inp["negatives"].items():
        if any(re.match(ln["match"], name) for ln in cfg["lines"]):
            warnings.append(f"{name}: Epos Now shows stock of {raw}; counted as 0 - check the real count")

    rows, periods, pinfo, line_info, meta = compute(cfg, quarters, partial_q, stock, on_order, holding, warnings)

    stamp = datetime.now().strftime("%Y-%m-%d")
    meta.update({"when": datetime.now().strftime("%Y-%m-%d %H:%M"), "stock_file": stock_file,
                 "files": files, "warnings": warnings})
    xlsx = out_dir / f"stock_forecast_{qlabel(max(quarters))}_{stamp}.xlsx"
    write_excel(xlsx, rows, periods, pinfo, line_info, meta)
    shutil.copyfile(xlsx, out_dir / "stock_forecast_latest.xlsx")
    write_csv(out_dir / "stock_forecast_latest.csv", rows, periods, pinfo)

    print_report(rows, line_info, pinfo, periods, meta, warnings, stock_file, args.only_orders)
    print(f"\nSaved: {xlsx}\n       output/stock_forecast_latest.xlsx and .csv")


if __name__ == "__main__":
    main()
