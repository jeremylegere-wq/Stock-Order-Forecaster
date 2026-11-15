# SPDX-License-Identifier: GPL-3.0-or-later
"""
Engine: a thin, GUI-friendly layer over forecast.py.

forecast.py stays the single source of truth for the maths (so the dashboard, the Excel
report and the command-line tool can never disagree). This module adds:

  * load_all()   - reads config + the database once
  * run()        - runs the forecast with scenario overrides (buffer, on-order, delivery term)
  * tables       - pandas DataFrames for charts
  * simulate()   - a bootstrap risk simulation (numpy) for "what if I order N units?"
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

import forecast as f
import store

ROOT = f.ROOT
DATA = ROOT / "data"


class DataError(Exception):
    """Something is wrong with the input files (shown to the user, not a crash)."""


@dataclass
class Data:
    cfg: dict
    quarters: dict            # {(year, q): {product: units}}
    terms: dict               # {(year, q): {source, units, file_name, imported_at, ...}}
    stock: dict               # newest stock report, negatives counted as 0
    on_order: dict
    holding: dict
    snapshots: list           # [{snapshot_id, taken_at, confirmed, levels}]
    latest: dict | None       # the newest snapshot
    negatives: dict           # {product: raw negative stock}
    costs: dict
    partial_cfg: set
    load_warnings: list = field(default_factory=list)
    root: Path = ROOT
    enrolments: dict = field(default_factory=dict)        # {year: total students}
    enrolment_grid: list = field(default_factory=list)
    annotations: list = field(default_factory=list)

    @property
    def stock_taken(self):
        return pd.Timestamp(self.latest["taken_at"]) if self.latest else None

    @property
    def stock_age_days(self):
        return (pd.Timestamp.now().normalize() - self.stock_taken.normalize()).days if self.latest else None

    @property
    def stock_confirmed(self) -> bool:
        return bool(self.latest and self.latest["confirmed"])


def load_all(root: Path = ROOT) -> Data:
    """Read the config and the database (building the database from the CSV folders on first use)."""
    try:
        cfg = f.load_config(root / "config.toml")
        conn = store.ensure_ready(root)
        inp = store.load_inputs(conn)
        conn.close()
    except SystemExit as e:                       # forecast.py reports config problems with sys.exit(msg)
        raise DataError(str(e)) from None
    except store.StoreError as e:
        raise DataError(str(e)) from None
    if not inp["quarters"]:
        raise DataError("No sales have been loaded yet. Add a term's sales on the Data & sync tab.")
    if not inp["latest"]:
        raise DataError("No stock report has been loaded yet. Add one on the Data & sync tab.")
    warnings: list[str] = []
    partial = {q.upper() for q in cfg["settings"].get("partial_quarters", [])} | f.auto_partial(inp["quarters"], warnings)
    return Data(cfg, inp["quarters"], inp["terms"], inp["stock"], inp["on_order"], inp["holding"], inp["snapshots"],
                inp["latest"], inp["negatives"], inp["costs"], partial, warnings, root,
                inp["enrolments"], inp["enrolment_grid"], inp["annotations"])


@dataclass
class Result:
    items: pd.DataFrame
    periods: list
    pinfo: dict
    line_info: dict
    meta: dict
    warnings: list
    quarters: dict
    raw: tuple = None          # the untouched forecast.py output, for the Excel writer


def run(d: Data, buffer_pct: float | None = None, include_on_order: bool | None = None,
        delivery: dict | None = None, partial: set | None = None) -> Result:
    """Run the forecast. `delivery` maps line name -> 1-4, or 0/None for 'no deduction'.
    Lines not in `delivery` keep what config.toml says."""
    cfg = copy.deepcopy(d.cfg)
    if buffer_pct is not None:
        cfg["settings"]["buffer_pct"] = buffer_pct
    if include_on_order is not None:
        cfg["settings"]["include_on_order"] = include_on_order
    for ln in cfg["lines"]:
        if delivery and ln["name"] in delivery:
            v = delivery[ln["name"]]
            if v:
                ln["delivery_quarter"] = int(v)
            else:
                ln.pop("delivery_quarter", None)
    partial_q = (d.partial_cfg if partial is None else {p.upper() for p in partial})
    warnings = list(d.load_warnings)
    try:
        rows, periods, pinfo, line_info, meta = f.compute(
            cfg, d.quarters, partial_q, d.stock, d.on_order, d.holding, warnings)
    except SystemExit as e:
        raise DataError(str(e)) from None
    df = _items_frame(rows, periods, pinfo)
    if not df.empty:
        df["stock_check"] = df["name"].isin(d.negatives)
        df["unit_cost"] = df["name"].map(d.costs).fillna(0.0)
        df["order_cost"] = df["order"] * df["unit_cost"]
    return Result(df, periods, pinfo, line_info, meta, warnings, d.quarters, (rows, periods, pinfo, line_info, meta))


def _items_frame(rows, periods, pinfo) -> pd.DataFrame:
    recs = []
    for it in rows:
        r = {"line": it["line"], "size": it["size"], "name": it["name"], "avg": it["avg"], "unc": it["unc"],
             "target": it["target"], "stock": it["stock"], "on_order": it["on_order"],
             "holding": it["holding"], "pre_delivery": it["expected"], "available": it["available"],
             "order": it["order"] if it["order"] is not None else 0, "has_history": it["target"] is not None}
        for p in periods:
            r[f.period_label(p)] = it["vals"][p]
        recs.append(r)
    df = pd.DataFrame(recs)
    if df.empty:
        return df
    df["target"] = df["target"].fillna(0).astype(int)
    # how well covered is each item? 1.0 = at target
    df["cover"] = np.where(df["target"] > 0, (df["available"] / df["target"]).clip(lower=0), np.nan)
    df["status"] = np.select(
        [~df["has_history"], df["order"] > 0, df["cover"] >= 1.5],
        ["No history", "Order", "Overstocked"], default="OK")
    return df


def period_columns(res: Result) -> list[str]:
    return [f.period_label(p) for p in res.periods]


# --------------------------------------------------------------------------- tables for charts
def quarterly_matrix(res: Result, line: str) -> pd.DataFrame:
    """Rows = sizes, columns = quarters (oldest -> newest), values = units sold."""
    names = res.items.loc[res.items["line"] == line, ["name", "size"]]
    qs = sorted(res.quarters)
    data = {f.qlabel(k): [res.quarters[k].get(n, 0) for n in names["name"]] for k in qs}
    return pd.DataFrame(data, index=names["size"].values)


def line_totals_by_quarter(res: Result, line: str) -> pd.Series:
    m = quarterly_matrix(res, line)
    return m.sum(axis=0)


def season_share(res: Result, line: str) -> pd.DataFrame:
    """Share of each year's sales that fall in each term (T1..T4), for complete years only."""
    rows = []
    for p in res.periods:
        if not res.pinfo[p]["complete"]:
            continue
        keys = res.pinfo[p]["present"]
        names = res.items.loc[res.items["line"] == line, "name"]
        tot = {k: sum(res.quarters[k].get(n, 0) for n in names) for k in keys}
        s = sum(tot.values())
        if s:
            for (y, q), u in tot.items():
                rows.append({"period": f.period_label(p), "term": f"T{q}", "share": u / s})
    return pd.DataFrame(rows)


def per_100_students(res: Result, d: Data, line: str) -> pd.DataFrame:
    """Units sold per 100 students for each complete year (T4 to T3). Year Y's period is matched to the
    August census of calendar year Y."""
    names = res.items.loc[res.items["line"] == line, "name"]
    rows = []
    for p in res.periods:
        if not res.pinfo[p]["complete"]:
            continue
        units = sum(res.quarters[k].get(n, 0) for k in res.pinfo[p]["present"] for n in names)
        first = res.line_info.get(line, {}).get("first")
        if first is not None and p < first:
            continue
        kids = d.enrolments.get(p)
        rows.append({"period": f.period_label(p), "year": p, "units": units, "students": kids,
                     "per100": (units / kids * 100) if kids else None})
    return pd.DataFrame(rows)


def stock_trail(d: Data, line: str, res: Result) -> pd.DataFrame:
    names = res.items.loc[res.items["line"] == line, "name"]
    return pd.DataFrame([{"date": pd.Timestamp(s["taken_at"]), "confirmed": s["confirmed"],
                          "units": sum(max(0, s["levels"].get(n, 0)) for n in names)} for s in d.snapshots])


# --------------------------------------------------------------------------- risk simulation
def simulate(annual: list[int], stock_at_delivery: int, order: int, n: int = 20000, seed: int = 7):
    """Bootstrap the season's demand from past complete years (+ smoothing noise) and report how
    often `stock_at_delivery + order` would have run out.

    With only 3-8 years of history this is indicative, not exact: it tells you roughly how
    exposed you are, and how much each extra unit of order buys you.
    Returns (draws, probability_of_stockout)."""
    a = np.asarray([x for x in annual if x is not None], dtype=float)
    if a.size == 0:
        return np.array([]), float("nan")
    rng = np.random.default_rng(seed)
    pick = rng.choice(a, size=n, replace=True)
    # smoothed bootstrap: Silverman-style bandwidth so we don't just replay 3-8 exact numbers
    if a.size > 1:
        sd = a.std(ddof=1)
        iqr = np.subtract(*np.percentile(a, [75, 25])) / 1.34
        bw = 0.9 * min(sd, iqr if iqr > 0 else sd) * a.size ** (-0.2)
    else:
        bw = max(1.0, 0.15 * a.mean())
    draws = np.clip(pick + rng.normal(0, max(bw, 0.5), size=n), 0, None)
    supply = stock_at_delivery + order
    return draws, float((draws > supply).mean())


def prob_curve(annual: list[int], stock_at_delivery: int, max_order: int, step: int = 1):
    xs = np.arange(0, max_order + 1, step)
    draws, _ = simulate(annual, stock_at_delivery, 0)
    if draws.size == 0:
        return xs, np.full(xs.shape, np.nan)
    return xs, np.array([(draws > stock_at_delivery + x).mean() for x in xs])


def annual_values(res: Result, name: str, complete_only=True) -> list[int]:
    row = res.items.loc[res.items["name"] == name]
    if row.empty:
        return []
    out = []
    for p in res.periods:
        v = row.iloc[0][f.period_label(p)]
        if v is None or (isinstance(v, float) and np.isnan(v)):
            continue
        if complete_only and not res.pinfo[p]["complete"]:
            continue
        out.append(int(v))
    return out


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)
