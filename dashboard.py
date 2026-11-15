#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Stock planner - interactive dashboard (Plotly Dash).

    python dashboard.py            # opens http://127.0.0.1:8050 in your browser

It uses exactly the same forecast engine as forecast.py, so the numbers match the Excel report.
Everything runs on your own computer; nothing is sent anywhere.
"""
from __future__ import annotations

import argparse
import base64
import tempfile
import threading
import webbrowser
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import ALL, Dash, Input, Output, State, ctx, dash_table, dcc, html, no_update

import engine as eng
import forecast as f
import store

# ----------------------------------------------------------------------------- look & feel
BLUE, LBLUE, TEAL, ORANGE = "#2563eb", "#93c5fd", "#0d9488", "#ea580c"
GREY, DGREY, GREEN, RED, AMBER = "#94a3b8", "#475569", "#16a34a", "#dc2626", "#d97706"
TERM_COL = {"T1": "#2563eb", "T2": "#0d9488", "T3": "#94a3b8", "T4": "#ea580c"}
FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"

CSS = """
*{box-sizing:border-box} body{margin:0;font-family:%s;background:#f6f7f9;color:#1e293b}
.top{background:#0f172a;color:#fff;padding:14px 22px;display:flex;gap:18px;align-items:baseline;flex-wrap:wrap}
.top h1{font-size:19px;margin:0;font-weight:650} .top .sub{font-size:13px;color:#cbd5e1}
.wrap{display:grid;grid-template-columns:290px 1fr;gap:18px;padding:18px 22px;align-items:start}
.side{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:16px;position:sticky;top:12px}
.side h3{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#64748b;margin:16px 0 8px}
.side h3:first-child{margin-top:0}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px;margin-bottom:16px}
.card h2{font-size:15px;margin:0 0 4px} .muted{color:#64748b;font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px}
.kpi{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px}
.kpi .v{font-size:26px;font-weight:700;line-height:1.1} .kpi .l{font-size:12px;color:#64748b;margin-top:2px}
.kpi.hot .v{color:%s}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:16px} .row2>.card{margin-bottom:16px}
button.btn{background:%s;color:#fff;border:0;border-radius:7px;padding:8px 14px;font-size:14px;cursor:pointer;margin-right:8px}
button.btn.alt{background:#fff;color:#0f172a;border:1px solid #cbd5e1}
.warn{background:#fffbeb;border:1px solid #fde68a;border-radius:8px;padding:8px 12px;font-size:13px;margin:6px 0}
.ok{background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:8px 12px;font-size:13px;margin:6px 0}
.err{background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:8px 12px;font-size:13px;margin:6px 0}
details summary{cursor:pointer;font-size:13px;color:#334155;margin:6px 0}
.dqrow{font-size:12px;margin:6px 0 2px;color:#334155}
@media (max-width:980px){.wrap{grid-template-columns:1fr}.side{position:static}.row2{grid-template-columns:1fr}}
@media print{.side,.noprint,.tab-parent,button,.top .sub{display:none!important}.wrap{display:block;padding:0}
 .card,.kpi{break-inside:avoid;border:1px solid #ddd}body{background:#fff}}
""" % (FONT, ORANGE, BLUE)


def style_fig(fig: go.Figure, title: str | None = None, h: int = 420) -> go.Figure:
    fig.update_layout(template="plotly_white", font=dict(family=FONT, size=13, color="#1e293b"),
                      margin=dict(l=50, r=20, t=50 if title else 20, b=45), height=h,
                      title=dict(text=title, x=0, font=dict(size=15)) if title else None,
                      legend=dict(orientation="h", y=-0.18, x=0), hoverlabel=dict(font_size=13))
    return fig


def empty_fig(msg: str, h: int = 300) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(text=msg, showarrow=False, font=dict(size=14, color=DGREY))
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return style_fig(fig, h=h)


# ----------------------------------------------------------------------------- state
STATE: dict = {"data": None, "error": None}


def reload_data() -> None:
    get_result.cache_clear()
    try:
        STATE["data"], STATE["error"] = eng.load_all(), None
    except eng.DataError as e:
        STATE["data"], STATE["error"] = None, str(e)


@lru_cache(maxsize=64)
def get_result(version: int, buffer: float, onorder: bool, dq: tuple):
    return eng.run(STATE["data"], buffer_pct=buffer, include_on_order=onorder, delivery=dict(dq))


def scenario(version, buffer, onorder, dq_vals, dq_ids):
    """Turn the sidebar controls into a forecast result."""
    delivery = {}
    for val, id_ in zip(dq_vals or [], dq_ids or []):
        if val in (None, "cfg"):
            continue
        delivery[id_["line"]] = int(val)
    return get_result(version or 0, float(buffer), bool(onorder and "on" in onorder), tuple(sorted(delivery.items())))


reload_data()
D = STATE["data"]
LINES = [ln["name"] for ln in D.cfg["lines"]] if D else []
CFG_BUFFER = D.cfg["settings"].get("buffer_pct", 10) if D else 10
CFG_ONORDER = D.cfg["settings"].get("include_on_order", True) if D else True


# ----------------------------------------------------------------------------- layout
def dq_dropdowns():
    out = []
    cfg_dq = {ln["name"]: ln.get("delivery_quarter") for ln in D.cfg["lines"]} if D else {}
    opts = [{"label": "As in config.toml", "value": "cfg"}, {"label": "No deduction", "value": "0"},
            {"label": "Arrives Term 1", "value": "1"}, {"label": "Arrives Term 2 (April)", "value": "2"},
            {"label": "Arrives Term 3", "value": "3"}, {"label": "Arrives Term 4 (October)", "value": "4"}]
    for ln in LINES:
        now = cfg_dq.get(ln)
        out.append(html.Div(f"{ln}  (config: {'Term ' + str(now) if now else 'none'})", className="dqrow"))
        out.append(dcc.Dropdown(id={"type": "dq", "line": ln}, options=opts, value="cfg", clearable=False,
                                style={"fontSize": "13px"}))
    return out


def sidebar():
    return html.Div(className="side", children=[
        html.H3("Scenario"),
        html.Div("Safety buffer on top of the best year", className="muted"),
        dcc.Slider(id="buffer", min=0, max=40, step=1, value=CFG_BUFFER,
                   marks={0: "0%", 10: "10%", 20: "20%", 30: "30%", 40: "40%"}, tooltip={"always_visible": False}),
        html.Div(style={"height": "10px"}),
        dcc.Checklist(id="onorder", options=[{"label": " Count stock already on order", "value": "on"}],
                      value=["on"] if CFG_ONORDER else []),
        html.H3("Show"),
        dcc.Dropdown(id="lines-filter", options=LINES, value=[], multi=True, placeholder="All product lines"),
        html.Div(style={"height": "8px"}),
        dcc.Checklist(id="only-orders", options=[{"label": " Only items that need an order", "value": "y"}],
                      value=["y"]),
        html.H3("When does each order arrive?"),
        html.Div("Sales expected between now and delivery are taken off stock first.", className="muted"),
        html.Details([html.Summary("Per-line delivery term"), *dq_dropdowns()]),
        html.H3("Data"),
        html.Div(id="side-status", className="muted"),
    ])


def tab_order():
    return html.Div([
        html.Div(id="kpis", className="kpis"),
        html.Div(className="card", children=[
            html.H2("What to order"),
            html.Div(id="summary-box"),
            html.Div(className="noprint", style={"marginTop": "10px"}, children=[
                html.Button("Download Excel", id="btn-xlsx", className="btn"),
                html.Button("Print / save as PDF", id="btn-print", className="btn alt"),
                dcc.Download(id="dl-xlsx")]),
        ]),
        html.Div(className="card", children=[dcc.Graph(id="order-bar", config={"displayModeBar": False})]),
        html.Div(className="card", children=[
            html.H2("Item by item"), html.Div("Click a column heading to sort; type in the filter row to search.", className="muted"),
            dash_table.DataTable(
                id="order-table", page_size=25, sort_action="native", filter_action="native",
                export_format="csv", style_as_list_view=True,
                style_header={"fontWeight": "600", "backgroundColor": "#f1f5f9"},
                style_cell={"fontFamily": FONT, "fontSize": 13, "padding": "6px 8px", "textAlign": "right"},
                style_cell_conditional=[{"if": {"column_id": c}, "textAlign": "left"} for c in ("Line", "Size", "Status", "Stock note")],
                style_data_conditional=[
                    {"if": {"filter_query": "{Order} > 0"}, "backgroundColor": "#fff4ec"},
                    {"if": {"filter_query": "{Order} > 0", "column_id": "Order"}, "fontWeight": "700", "color": ORANGE},
                    {"if": {"filter_query": '{Status} = "Overstocked"', "column_id": "Status"}, "color": AMBER},
                    {"if": {"filter_query": '{Status} = "No history"', "column_id": "Status"}, "color": GREY}]),
        ]),
    ])


def tab_flow():
    return html.Div([
        html.Div(className="card", children=[
            html.H2("Where the stock comes from and where it goes"),
            html.Div("Pick a product line. Bars show what we will hold when the order lands against the target; "
                     "the flow diagram shows the same units moving through each size.", className="muted"),
            dcc.Dropdown(id="flow-line", options=LINES, value=LINES[0] if LINES else None, clearable=False,
                         style={"maxWidth": "360px", "marginTop": "8px"})]),
        html.Div(className="card", children=[dcc.Graph(id="flow-bars")]),
        html.Div(className="row2", children=[
            html.Div(className="card", children=[dcc.Graph(id="flow-waterfall")]),
            html.Div(className="card", children=[dcc.Graph(id="flow-trail")])]),
        html.Div(className="card", children=[dcc.Graph(id="flow-sankey")]),
    ])


def tab_history():
    return html.Div([
        html.Div(className="card", children=[
            html.H2("Demand history"),
            html.Div("What actually sold, by size and term, and how the shop's year is shaped.", className="muted"),
            dcc.Dropdown(id="hist-line", options=LINES, value=LINES[0] if LINES else None, clearable=False,
                         style={"maxWidth": "360px", "marginTop": "8px"})]),
        html.Div(className="card", children=[dcc.Graph(id="hist-heat")]),
        html.Div(className="row2", children=[
            html.Div(className="card", children=[dcc.Graph(id="hist-annual")]),
            html.Div(className="card", children=[dcc.Graph(id="hist-season")])]),
        html.Div(className="card", children=[dcc.Graph(id="hist-quarter")]),
    ])


def tab_item():
    return html.Div([
        html.Div(className="card", children=[
            html.H2("Item explorer and risk check"),
            html.Div("Choose one item and try different order quantities. The simulation replays past years' demand "
                     "(with a little smoothing) to estimate how often the shop would have run out.", className="muted"),
            dcc.Dropdown(id="item-pick", clearable=False, style={"maxWidth": "480px", "marginTop": "8px"}),
            html.Div(style={"height": "14px"}),
            html.Div("If we order this many units:", className="muted"),
            dcc.Slider(id="risk-order", min=0, max=100, step=1, value=0, tooltip={"always_visible": True}),
            html.Div(id="risk-kpi", className="kpis", style={"marginTop": "14px"})]),
        html.Div(className="card", children=[dcc.Graph(id="item-annual")]),
        html.Div(className="row2", children=[
            html.Div(className="card", children=[dcc.Graph(id="risk-hist")]),
            html.Div(className="card", children=[dcc.Graph(id="risk-curve")])]),
        html.Div(className="muted", style={"marginBottom": "16px"}, children=
                 "Treat the risk figures as a guide. With 3-8 years of history the true chance of running out can't be "
                 "pinned down precisely, and one-off events (a new product, a change in headcount) are not in the history."),
    ])


UPLOAD_STYLE = {"border": "2px dashed #cbd5e1", "borderRadius": "8px", "padding": "18px", "textAlign": "center",
                "color": DGREY, "cursor": "pointer"}
TERM_OPTS = [{"label": f"Term {i}", "value": i} for i in (1, 2, 3, 4)]
TABLE_STYLE = dict(style_cell={"fontFamily": FONT, "fontSize": 13, "textAlign": "left", "padding": "5px 8px"},
                   style_header={"fontWeight": "600", "backgroundColor": "#f1f5f9"})


def tab_data():
    return html.Div([
        html.Div(className="card", children=[html.H2("Data in use"), html.Div(id="data-status")]),
        html.Div(className="row2", children=[
            html.Div(className="card", children=[
                html.H2("Add a term's sales"),
                html.Div("Drop the Epos Now 'Sales by Product' CSV here and say which term it is. It is added to the database "
                         "(a term that is already there is replaced; the old load stays in the log). No renaming needed.", className="muted"),
                html.Div(style={"display": "flex", "gap": "8px", "margin": "10px 0"}, children=[
                    dcc.Input(id="up-year", type="number", value=datetime.now().year, min=2018, max=2100, style={"width": "90px"}),
                    dcc.Dropdown(id="up-term", options=TERM_OPTS, value=1, clearable=False, style={"width": "120px"})]),
                dcc.Upload(id="up-sales", children=html.Div("Drop sales CSV here or click to choose"), style=UPLOAD_STYLE),
                dcc.Loading(html.Div(id="up-sales-msg"))]),
            html.Div(className="card", children=[
                html.H2("Add a stock report"),
                html.Div("Drop the Epos Now 'Stock Levels' CSV here. Every report is kept; the newest is the one used. "
                         "Tick the box only if the stock manager has confirmed these figures (for example after a stocktake).", className="muted"),
                html.Div(style={"display": "flex", "gap": "8px", "margin": "10px 0", "alignItems": "center"}, children=[
                    dcc.Checklist(id="up-confirmed", options=[{"label": " Confirmed by the stock manager", "value": "y"}], value=[]),
                    dcc.Input(id="up-confirmed-by", type="text", placeholder="confirmed by", style={"width": "130px"})]),
                dcc.Upload(id="up-stock", children=html.Div("Drop stock CSV here or click to choose"), style=UPLOAD_STYLE),
                html.Div(style={"marginTop": "8px"}, children=[html.Button("Mark the newest report as confirmed", id="btn-confirm", className="btn alt")]),
                dcc.Loading(html.Div(id="up-stock-msg"))])]),
        html.Div(className="row2", children=[
            html.Div(className="card", children=[
                html.H2("Stock held outside Epos Now"),
                html.Div("For example, stock a supplier is holding for you. Product names must match Epos Now exactly.", className="muted"),
                dash_table.DataTable(id="holding-table", editable=True, row_deletable=True,
                                     columns=[{"name": "Name", "id": "Name"}, {"name": "Qty", "id": "Qty", "type": "numeric"}], **TABLE_STYLE),
                html.Div(style={"marginTop": "8px"}, children=[html.Button("Add row", id="hold-add", className="btn alt"),
                                                               html.Button("Save", id="hold-save", className="btn")]),
                html.Div(id="hold-msg")]),
            html.Div(className="card", children=[
                html.H2("Pull from Epos Now"),
                html.Div(id="sync-info", className="muted"),
                html.Div(style={"display": "flex", "gap": "8px", "margin": "10px 0"}, children=[
                    dcc.Input(id="sync-year", type="number", value=datetime.now().year, style={"width": "90px"}),
                    dcc.Dropdown(id="sync-term", options=TERM_OPTS, value=1, clearable=False, style={"width": "120px"})]),
                html.Button("Pull sales for this term", id="sync-sales", className="btn"),
                html.Button("Pull stock levels", id="sync-stock", className="btn alt"),
                dcc.Loading(html.Div(id="sync-msg"))])]),
        html.Div(className="card", children=[
            html.H2("Backup and export"),
            html.Div(id="backup-info", className="muted"),
            html.Div(style={"marginTop": "10px"}, children=[
                html.Button("Download full backup (zip of CSVs)", id="btn-backup", className="btn"),
                html.Button("Write backup now", id="btn-backup-now", className="btn alt"),
                dcc.Download(id="dl-backup")]),
            html.Div(id="backup-msg")]),
    ])


def tab_context():
    return html.Div([
        html.Div(className="card", children=[
            html.H2("Context"),
            html.Div("Enrolments and notes you enter here are stored in the database and included in every backup. "
                     "They explain the sales history: a bigger customer base sells more.", className="muted")]),
        html.Div(className="card", children=[dcc.Graph(id="ctx-enrol")]),
        html.Div(className="card", children=[
            dcc.Dropdown(id="ctx-line", options=LINES, value=LINES[0] if LINES else None, clearable=False,
                         style={"maxWidth": "360px", "marginBottom": "8px"}),
            dcc.Graph(id="ctx-per100"),
            html.Div("Each year runs Term 4 to Term 3 and is matched to that year's August enrolment. The forecast does not adjust for enrolment; "
                     "this chart shows whether sales per student have been steady.", className="muted")]),
        html.Div(className="row2", children=[
            html.Div(className="card", children=[
                html.H2("Enter enrolments"),
                html.Div("Type the August census figures. Leave a cell empty if the year levels aren't known; a year with only a Total is fine.", className="muted"),
                dash_table.DataTable(id="enrol-table", editable=True, **TABLE_STYLE),
                html.Div(style={"display": "flex", "gap": "8px", "margin": "10px 0", "alignItems": "center"}, children=[
                    dcc.Input(id="enrol-new-year", type="number", placeholder="new year", style={"width": "100px"}),
                    html.Button("Add year", id="enrol-add", className="btn alt"),
                    html.Button("Save enrolments", id="enrol-save", className="btn")]),
                dcc.Loading(html.Div(id="enrol-msg"))]),
            html.Div(className="card", children=[
                html.H2("Notes on one-off events"),
                html.Div("For example: 'Hoodies sold out in Term 2', 'new product launched', 'big customer event in Term 1'. "
                         "They appear on the Demand history chart.", className="muted"),
                html.Div(style={"display": "flex", "gap": "8px", "margin": "10px 0", "flexWrap": "wrap"}, children=[
                    dcc.Input(id="note-year", type="number", value=datetime.now().year, style={"width": "90px"}),
                    dcc.Dropdown(id="note-term", options=[{"label": "Whole year", "value": 0}, *TERM_OPTS], value=0, clearable=False, style={"width": "150px"}),
                    dcc.Input(id="note-text", type="text", placeholder="what happened?", style={"flex": "1", "minWidth": "180px"}),
                    html.Button("Add note", id="note-add", className="btn")]),
                dash_table.DataTable(id="notes-table", row_deletable=True, columns=[
                    {"name": "ID", "id": "ID"}, {"name": "Year", "id": "Year"}, {"name": "Term", "id": "Term"}, {"name": "Note", "id": "Note"}], **TABLE_STYLE),
                html.Div(style={"marginTop": "8px"}, children=[html.Button("Save deletions", id="notes-save", className="btn alt")]),
                dcc.Loading(html.Div(id="notes-msg"))])]),
    ])


app = Dash(__name__, title="Stock Planner", suppress_callback_exceptions=True)
server = app.server
app.index_string = ("<!DOCTYPE html><html><head>{%metas%}<title>{%title%}</title>{%favicon%}{%css%}"
                    f"<style>{CSS}</style></head><body>{{%app_entry%}}<footer>{{%config%}}{{%scripts%}}{{%renderer%}}</footer></body></html>")

app.layout = html.Div([
    dcc.Store(id="version", data=0),
    html.Div(className="top", children=[
        html.H1("Stock Planner"),
        html.Div(id="top-status", className="sub", style={"marginLeft": "auto"})]),
    html.Div(className="wrap", children=[
        sidebar(),
        html.Div([
            html.Div(id="global-msg"),
            dcc.Tabs(id="tabs", value="order", parent_className="tab-parent", children=[
                dcc.Tab(label="Order sheet", value="order", children=html.Div(tab_order(), style={"paddingTop": "14px"})),
                dcc.Tab(label="Stock flow", value="flow", children=html.Div(tab_flow(), style={"paddingTop": "14px"})),
                dcc.Tab(label="Demand history", value="hist", children=html.Div(tab_history(), style={"paddingTop": "14px"})),
                dcc.Tab(label="Item & risk", value="item", children=html.Div(tab_item(), style={"paddingTop": "14px"})),
                dcc.Tab(label="Context", value="context", children=html.Div(tab_context(), style={"paddingTop": "14px"})),
                dcc.Tab(label="Data & sync", value="data", children=html.Div(tab_data(), style={"paddingTop": "14px"})),
            ])])]),
])

SCEN = [Input("version", "data"), Input("buffer", "value"), Input("onorder", "value"),
        Input({"type": "dq", "line": ALL}, "value"), State({"type": "dq", "line": ALL}, "id")]


def no_data_notice():
    return html.Div([html.Div("The data could not be loaded:", style={"fontWeight": 600}),
                     html.Div(STATE["error"] or "Unknown problem.")], className="err")


def pick_lines(df: pd.DataFrame, lines, only_orders) -> pd.DataFrame:
    if lines:
        df = df[df["line"].isin(lines)]
    if only_orders:
        df = df[df["order"] > 0]
    return df


# ----------------------------------------------------------------------------- header / sidebar status
def freshness_notes(d, res) -> list:
    """Plain-language warnings about how far to trust the stock figures."""
    out = []
    when = d.stock_taken
    age = d.stock_age_days
    if not d.stock_confirmed:
        out.append(html.Div(f"The stock report ({when:%d %b %Y}) has not been confirmed by the stock manager. Stock in Epos Now is often only "
                            "updated when an order is being prepared, so check the figures with them before ordering.", className="warn"))
    if age is not None and age > 30:
        out.append(html.Div(f"The stock report is {age} days old.", className="warn"))
    shown = set(res.items["name"])
    relevant = [n for n in d.negatives if n in shown]
    if relevant:
        out.append(html.Div(f"{len(relevant)} item(s) in the order list show negative stock in Epos Now and are counted as 0. "
                            f"Their count needs checking: {', '.join(relevant[:6])}.", className="warn"))
    return out


@app.callback(Output("top-status", "children"), Output("side-status", "children"), Output("global-msg", "children"),
              Input("version", "data"))
def status(version):
    if STATE["error"]:
        return "", "", no_data_notice()
    d = STATE["data"]
    complete = [k for k in d.quarters if f.qlabel(k) not in d.partial_cfg]
    last = f.qlabel(max(complete)) if complete else "none"
    seeded = sum(1 for t in d.terms.values() if t["source"] == "seeded")
    conf = "confirmed" if d.stock_confirmed else "not confirmed"
    top = f"Sales to {last}  |  stock report {d.stock_taken:%d %b %Y} ({conf})"
    side = (f"{len(d.terms)} terms of sales in the database ({seeded} copied from an old spreadsheet). "
            f"Stock report from {d.stock_taken:%d %b %Y}, {d.stock_age_days} day(s) old, {conf}.")
    return top, side, None


# ----------------------------------------------------------------------------- tab 1: order sheet
@app.callback(Output("kpis", "children"), Output("summary-box", "children"), Output("order-bar", "figure"),
              Output("order-table", "data"), Output("order-table", "columns"),
              *SCEN, Input("lines-filter", "value"), Input("only-orders", "value"))
def order_sheet(version, buffer, onorder, dq_vals, dq_ids, lines, only_orders):
    if STATE["error"]:
        return [], no_data_notice(), empty_fig("No data"), [], []
    res = scenario(version, buffer, onorder, dq_vals, dq_ids)
    all_ = res.items
    need = all_[all_["order"] > 0]
    kpis = [
        html.Div([html.Div(f"{len(need)}", className="v"), html.Div(f"items to order (of {len(all_)})", className="l")], className="kpi hot"),
        html.Div([html.Div(f"{int(need['order'].sum()):,}", className="v"), html.Div("units in total", className="l")], className="kpi hot"),
        html.Div([html.Div(f"${need['order_cost'].sum():,.0f}", className="v"), html.Div("approx. cost (POS cost prices)", className="l")], className="kpi"),
        html.Div([html.Div(f"{need['line'].nunique()}", className="v"), html.Div("product lines affected", className="l")], className="kpi"),
        html.Div([html.Div(f"{int((all_['status'] == 'Overstocked').sum())}", className="v"), html.Div("items 150%+ of target", className="l")], className="kpi"),
    ]
    # plain-English summary, one line per product line
    bullets = []
    for ln in LINES:
        g = need[need["line"] == ln].sort_values("order", ascending=False)
        if g.empty:
            continue
        top = ", ".join(f"{r['size']} ({int(r['order'])})" for _, r in g.head(3).iterrows())
        li = res.line_info.get(ln, {})
        when = f" Allows for {f.terms_text(li['window'])} sales before delivery." if li.get("window") else ""
        bullets.append(html.Li([html.B(ln), f": {int(g['order'].sum())} units across {len(g)} size{'s' if len(g) != 1 else ''}. Biggest: {top}.{when}"]))
    summary = [html.Ul(bullets, style={"margin": "6px 0 0 18px", "lineHeight": "1.6"}) if bullets
               else html.Div("Nothing needs ordering under this scenario.", className="ok")]
    summary = freshness_notes(STATE["data"], res) + summary
    summary += [html.Div(w, className="warn") for w in res.warnings[:4]]
    if (CFG_BUFFER, CFG_ONORDER) != (buffer, bool(onorder and "on" in onorder)) or any(v not in (None, "cfg") for v in dq_vals):
        summary.insert(0, html.Div("You are viewing a what-if scenario - it differs from config.toml.", className="warn"))

    view = pick_lines(all_, lines, only_orders)
    g = view.groupby("line", sort=False).agg(order=("order", "sum")).reset_index()
    g = g[g["order"] > 0] if only_orders else g
    fig = go.Figure(go.Bar(y=g["line"], x=g["order"], orientation="h", marker_color=ORANGE,
                           text=g["order"].astype(int), textposition="outside", hovertemplate="%{y}: %{x} units<extra></extra>"))
    fig.update_yaxes(autorange="reversed")
    fig.update_xaxes(title="Units to order")
    style_fig(fig, "Units to order by product line", h=max(260, 60 + 38 * len(g)))
    if g.empty:
        fig = empty_fig("Nothing to order for this selection")

    tbl = view[["line", "size", "stock", "on_order", "holding", "pre_delivery", "target", "order", "status", "order_cost"]].copy()
    tbl["order_cost"] = tbl["order_cost"].round(0)
    tbl["stock_check"] = np.where(view["stock_check"], "check count", "")
    cols = [("Line", "line"), ("Size", "size"), ("Stock", "stock"), ("On order", "on_order"), ("Held elsewhere", "holding"),
            ("Sales before delivery", "pre_delivery"), ("Target", "target"), ("Order", "order"), ("Status", "status"),
            ("Est. cost $", "order_cost"), ("Stock note", "stock_check")]
    tbl.columns = [c[0] for c in cols]
    columns = [{"name": c[0], "id": c[0], "type": "text" if c[0] in ("Line", "Size", "Status", "Stock note") else "numeric"} for c in cols]
    return kpis, summary, fig, tbl.to_dict("records"), columns


@app.callback(Output("dl-xlsx", "data"), Input("btn-xlsx", "n_clicks"), *SCEN, prevent_initial_call=True)
def download_excel(n, version, buffer, onorder, dq_vals, dq_ids):
    res = scenario(version, buffer, onorder, dq_vals, dq_ids)
    d = STATE["data"]
    rows, periods, pinfo, line_info, meta = res.raw
    meta = dict(meta, when=datetime.now().strftime("%Y-%m-%d %H:%M"),
                stock_file=f"database snapshot {d.stock_taken:%Y-%m-%d %H:%M}" + ("" if d.stock_confirmed else " (not confirmed by a stocktake)"),
                files={k: f"{t['source']}: {t['file_name'] or 'database'}" for k, t in d.terms.items()}, warnings=res.warnings)
    path = Path(tempfile.mkdtemp()) / f"stock_order_{datetime.now():%Y-%m-%d}.xlsx"
    f.write_excel(path, rows, periods, pinfo, line_info, meta)
    return dcc.send_file(str(path))


app.clientside_callback("function(n){ if(n){ window.print(); } return ''; }",
                        Output("btn-print", "title"), Input("btn-print", "n_clicks"))


# ----------------------------------------------------------------------------- tab 2: stock flow
@app.callback(Output("flow-bars", "figure"), Output("flow-waterfall", "figure"), Output("flow-trail", "figure"),
              Output("flow-sankey", "figure"), Input("flow-line", "value"), *SCEN)
def stock_flow(line, version, buffer, onorder, dq_vals, dq_ids):
    if STATE["error"] or not line:
        e = empty_fig("No data")
        return e, e, e, e
    res = scenario(version, buffer, onorder, dq_vals, dq_ids)
    use_oo = bool(onorder and "on" in onorder)
    g = res.items[(res.items["line"] == line) & res.items["has_history"]].copy()
    if g.empty:
        e = empty_fig("No sales history for this line yet")
        return e, e, e, e
    g["oo"] = g["on_order"] if use_oo else 0

    # 1) supply vs target, per size
    bars = go.Figure()
    for col, name, colour in (("stock", "In POS now", BLUE), ("oo", "Already on order", LBLUE),
                              ("holding", "Held outside POS", TEAL), ("order", "Suggested new order", ORANGE)):
        bars.add_bar(x=g["size"], y=g[col], name=name, marker_color=colour)
    bars.add_bar(x=g["size"], y=-g["pre_delivery"], name="Expected sales before delivery", marker_color=GREY)
    bars.add_trace(go.Scatter(x=g["size"], y=g["target"], mode="markers", name="Target",
                              marker=dict(symbol="diamond", size=11, color="#0f172a", line=dict(color="#fff", width=1)),
                              hovertemplate="Target %{y}<extra></extra>"))
    bars.update_layout(barmode="relative")
    bars.update_yaxes(title="Units")
    style_fig(bars, f"{line}: what we'll hold when the order lands, against the target", h=470)

    # 2) waterfall for the whole line
    t = g[["stock", "oo", "holding", "pre_delivery", "order", "target"]].sum()
    at_delivery = t["stock"] + t["oo"] + t["holding"] - t["pre_delivery"]
    wf = go.Figure(go.Waterfall(
        x=["In POS", "On order", "Held outside", "Sold before delivery", "At delivery", "New order", "Final position"],
        measure=["relative", "relative", "relative", "relative", "total", "relative", "total"],
        y=[t["stock"], t["oo"], t["holding"], -t["pre_delivery"], 0, t["order"], 0],
        connector={"line": {"color": GREY}}, increasing={"marker": {"color": BLUE}},
        decreasing={"marker": {"color": GREY}}, totals={"marker": {"color": DGREY}},
        text=[f"{v:,.0f}" for v in (t["stock"], t["oo"], t["holding"], -t["pre_delivery"], at_delivery, t["order"], at_delivery + t["order"])],
        textposition="outside"))
    wf.add_hline(y=t["target"], line_dash="dash", line_color=ORANGE)
    wf.update_yaxes(title="Units")
    style_fig(wf, f"{line}: stock flow, all sizes (dashed = target {t['target']:,.0f})", h=400)

    # 3) stock level at each saved stock report
    trail = eng.stock_trail(STATE["data"], line, res)
    if len(trail) >= 2:
        tf = go.Figure(go.Scatter(x=trail["date"], y=trail["units"], mode="lines+markers", line=dict(color=BLUE)))
        tf.add_hline(y=t["target"], line_dash="dash", line_color=ORANGE, annotation_text="Target")
        tf.update_yaxes(title="Units in POS")
        style_fig(tf, f"{line}: stock at each saved stock report", h=400)
    else:
        tf = empty_fig("Keep each term's stock report in data/stock<br>and this fills in as a stock level over time.", 400)
        style_fig(tf, f"{line}: stock over time", h=400)

    # 4) Sankey: supply sources -> sizes -> outcomes
    sizes = list(g["size"])
    src = ["In POS", "On order", "Held outside POS", "New order"]
    out = ["Sold before delivery", "Left on shelf at delivery"]
    labels = src + sizes + out
    o_idx = len(src) + len(sizes)
    S, T, V, C = [], [], [], []
    colours = [BLUE, LBLUE, TEAL, ORANGE]
    for j, (_, r) in enumerate(g.iterrows()):
        node = len(src) + j
        for k, col in enumerate(("stock", "oo", "holding", "order")):
            if r[col] > 0:
                S.append(k); T.append(node); V.append(float(r[col])); C.append(colours[k])
        supply = r["stock"] + r["oo"] + r["holding"] + r["order"]
        sold = min(r["pre_delivery"], supply)
        if sold > 0:
            S.append(node); T.append(o_idx); V.append(float(sold)); C.append(GREY)
        if supply - sold > 0:
            S.append(node); T.append(o_idx + 1); V.append(float(supply - sold)); C.append(GREEN)
    sk = go.Figure(go.Sankey(
        node=dict(label=labels, pad=12, thickness=16,
                  color=colours + ["#cbd5e1"] * len(sizes) + [GREY, GREEN]),
        link=dict(source=S, target=T, value=V, color=[c + "88" if len(c) == 7 else c for c in C])))
    style_fig(sk, f"{line}: units flowing from source, through each size, to outcome", h=max(420, 40 * len(sizes) + 120))
    return bars, wf, tf, sk


# ----------------------------------------------------------------------------- tab 3: history
@app.callback(Output("hist-heat", "figure"), Output("hist-annual", "figure"), Output("hist-season", "figure"),
              Output("hist-quarter", "figure"), Input("hist-line", "value"), *SCEN)
def history(line, version, buffer, onorder, dq_vals, dq_ids):
    if STATE["error"] or not line:
        e = empty_fig("No data")
        return e, e, e, e
    res = scenario(version, buffer, onorder, dq_vals, dq_ids)
    m = eng.quarterly_matrix(res, line)
    if m.empty or m.values.sum() == 0:
        e = empty_fig("No sales for this line")
        return e, e, e, e
    heat = go.Figure(go.Heatmap(z=m.values, x=list(m.columns), y=list(m.index), colorscale="Blues",
                                hovertemplate="%{y} in %{x}: %{z} units<extra></extra>",
                                texttemplate="%{z}" if m.shape[1] <= 20 else None, colorbar=dict(title="Units")))
    heat.update_yaxes(autorange="reversed")
    heat.update_xaxes(type="category", tickangle=-60)
    style_fig(heat, f"{line}: units sold, by size and term", h=max(320, 36 * len(m) + 160))

    g = res.items[res.items["line"] == line]
    plabels, totals, colours = [], [], []
    for p in res.periods:
        col = f.period_label(p)
        tot = g[col].fillna(0).sum()
        if tot == 0 and g[col].isna().all():
            continue
        plabels.append(col); totals.append(int(tot)); colours.append(BLUE if res.pinfo[p]["complete"] else GREY)
    ann = go.Figure(go.Bar(x=plabels, y=totals, marker_color=colours, text=totals, textposition="outside",
                           name="Units sold", hovertemplate="%{x}: %{y} units<extra></extra>"))
    if totals:
        ann.add_hline(y=float(np.mean([t for t, c in zip(totals, colours) if c == BLUE] or totals)), line_dash="dot",
                      line_color=DGREY, annotation_text="Average (complete years)")
    ann.add_hline(y=float(g["target"].sum()), line_dash="dash", line_color=ORANGE, annotation_text="Target (sum of sizes)")
    ann.update_yaxes(title="Units per year (T4 to T3)")
    style_fig(ann, f"{line}: units per year (grey = incomplete year)", h=400)

    sh = eng.season_share(res, line)
    if sh.empty:
        seas = empty_fig("Needs at least one complete year")
    else:
        mean = sh.groupby("term")["share"].mean().reindex(["T1", "T2", "T3", "T4"])
        seas = go.Figure(go.Bar(x=mean.index, y=mean.values * 100, marker_color=[TERM_COL[t] for t in mean.index],
                                text=[f"{v:.0f}%" for v in mean.values * 100], textposition="outside", opacity=0.55,
                                name="Average share"))
        seas.add_trace(go.Scatter(x=sh["term"], y=sh["share"] * 100, mode="markers", name="Each year",
                                  marker=dict(color="#0f172a", size=7), hovertemplate="%{text}: %{y:.0f}%<extra></extra>",
                                  text=sh["period"]))
        seas.update_yaxes(title="% of the year's sales")
    style_fig(seas, "How the year is shaped (share of sales by term)", h=400)

    qt = eng.line_totals_by_quarter(res, line)
    qf = go.Figure(go.Bar(x=list(qt.index), y=list(qt.values),
                          marker_color=[TERM_COL[f"T{i[-1]}"] for i in qt.index], hovertemplate="%{x}: %{y} units<extra></extra>"))
    for note in STATE["data"].annotations:
        lab = f"{note['year']}Q{note['term']}" if note["term"] else None
        if lab in list(qt.index):
            qf.add_annotation(x=lab, y=float(qt[lab]), text=note["note"][:40], showarrow=True, arrowhead=2, ay=-40,
                              font=dict(size=11), bgcolor="#fffbeb", bordercolor="#fde68a")
    qf.update_xaxes(type="category", tickangle=-60)
    qf.update_yaxes(title="Units")
    style_fig(qf, f"{line}: units sold each term (colour = term number)", h=360)
    return heat, ann, seas, qf


# ----------------------------------------------------------------------------- tab 4: item & risk
@app.callback(Output("item-pick", "options"), Output("item-pick", "value"), Input("version", "data"),
              State("item-pick", "value"))
def item_options(version, current):
    if STATE["error"]:
        return [], None
    res = scenario(version, CFG_BUFFER, ["on"] if CFG_ONORDER else [], [], [])
    df = res.items[res.items["has_history"]]
    opts = [{"label": f"{r['line']} - {r['size']}" + ("   (order " + str(int(r['order'])) + ")" if r["order"] else ""),
             "value": r["name"]} for _, r in df.iterrows()]
    vals = {o["value"] for o in opts}
    if current in vals:
        return opts, current
    first_need = df[df["order"] > 0]
    return opts, (first_need.iloc[0]["name"] if not first_need.empty else (opts[0]["value"] if opts else None))


@app.callback(Output("risk-order", "max"), Output("risk-order", "value"), Output("risk-order", "marks"),
              Input("item-pick", "value"), *SCEN)
def slider_range(name, version, buffer, onorder, dq_vals, dq_ids):
    if STATE["error"] or not name:
        return 100, 0, {}
    res = scenario(version, buffer, onorder, dq_vals, dq_ids)
    r = res.items[res.items["name"] == name].iloc[0]
    top = int(max(20, r["target"] * 1.3, r["order"] * 1.6))
    return top, int(r["order"]), {0: "0", int(r["order"]): "suggested", top: str(top)}


@app.callback(Output("item-annual", "figure"), Output("risk-hist", "figure"), Output("risk-curve", "figure"),
              Output("risk-kpi", "children"), Input("item-pick", "value"), Input("risk-order", "value"), *SCEN)
def item_risk(name, order_qty, version, buffer, onorder, dq_vals, dq_ids):
    if STATE["error"] or not name:
        e = empty_fig("No data")
        return e, e, e, []
    res = scenario(version, buffer, onorder, dq_vals, dq_ids)
    r = res.items[res.items["name"] == name].iloc[0]
    order_qty = int(order_qty or 0)
    # annual bars
    ann = go.Figure()
    labels, vals, cols = [], [], []
    for p in res.periods:
        v = r[f.period_label(p)]
        if v is None or (isinstance(v, float) and np.isnan(v)):
            continue
        labels.append(f.period_label(p)); vals.append(int(v)); cols.append(BLUE if res.pinfo[p]["complete"] else GREY)
    ann.add_bar(x=labels, y=vals, marker_color=cols, text=vals, textposition="outside", name="Units sold")
    if r["avg"] is not None and not pd.isna(r["avg"]):
        ann.add_hrect(y0=max(0, r["avg"] - r["unc"]), y1=r["avg"] + r["unc"], fillcolor=LBLUE, opacity=0.25, line_width=0,
                      annotation_text="average +/- uncertainty", annotation_position="top left")
    ann.add_hline(y=r["target"], line_color=ORANGE, line_dash="dash", annotation_text=f"Target {r['target']}")
    ann.add_hline(y=r["available"], line_color=GREEN, line_dash="dot", annotation_text=f"We'll have {r['available']}",
                  annotation_position="bottom right")
    ann.update_yaxes(title="Units per year (T4 to T3)")
    style_fig(ann, f"{r['line']} {r['size']}: history, target and what we'll hold", h=400)

    history_ = eng.annual_values(res, name)
    if len(history_) < 2:
        e = empty_fig("Not enough complete years to simulate (need 2+)")
        return ann, e, e, [html.Div([html.Div("n/a", className="v"), html.Div("risk needs 2+ complete years", className="l")], className="kpi")]
    draws, p_out = eng.simulate(history_, int(r["available"]), order_qty)
    supply = int(r["available"]) + order_qty
    hist = go.Figure(go.Histogram(x=draws, nbinsx=40, marker_color=LBLUE, name="Simulated year"))
    hist.add_vline(x=supply, line_color=RED, line_width=2, annotation_text=f"Stock we'd hold: {supply}", annotation_position="top right")
    hist.add_vline(x=r["target"], line_color=ORANGE, line_dash="dash", annotation_text="Target", annotation_position="bottom left")
    hist.update_xaxes(title="Units sold in a year (simulated)")
    hist.update_yaxes(title="Simulated years")
    style_fig(hist, f"Chance of running out: {p_out:.0%}", h=380)

    hi = int(max(r["target"] * 1.3, order_qty * 1.3, 20))
    xs, ys = eng.prob_curve(history_, int(r["available"]), hi)
    curve = go.Figure(go.Scatter(x=xs, y=ys * 100, mode="lines", line=dict(color=BLUE, width=3), name="Chance of running out"))
    curve.add_trace(go.Scatter(x=[order_qty], y=[p_out * 100], mode="markers", marker=dict(size=13, color=RED), name="Your choice"))
    if r["order"]:
        curve.add_vline(x=int(r["order"]), line_dash="dash", line_color=ORANGE, annotation_text="Suggested")
    curve.update_xaxes(title="Units ordered")
    curve.update_yaxes(title="% chance of running out", range=[0, 100])
    style_fig(curve, "What each extra unit buys", h=380)

    kpis = [html.Div([html.Div(f"{int(r['available'])}", className="v"), html.Div("we'll hold at delivery (before ordering)", className="l")], className="kpi"),
            html.Div([html.Div(f"{int(r['order'])}", className="v"), html.Div("suggested order", className="l")], className="kpi hot"),
            html.Div([html.Div(f"{p_out:.0%}", className="v"), html.Div(f"chance of running out if you order {order_qty}", className="l")], className="kpi hot"),
            html.Div([html.Div(f"{len(history_)}", className="v"), html.Div("complete years in the history", className="l")], className="kpi")]
    return ann, hist, curve, kpis


# ----------------------------------------------------------------------------- database helpers
def db():
    return store.ensure_ready(eng.ROOT)


def _decode(contents: str) -> bytes:
    return base64.b64decode(contents.split(",", 1)[1])


def _bump(version):
    reload_data()
    return (version or 0) + 1


def _msg(res: dict, cls: str = "ok"):
    items = [html.Div(res["message"], className=cls)]
    if res.get("backup"):
        items.append(html.Div(res["backup"], className="warn" if res["backup"].startswith("WARNING") else "muted"))
    return items


def _fail(e: Exception):
    return html.Div(str(e), className="err")


# ----------------------------------------------------------------------------- tab: data & sync
@app.callback(Output("data-status", "children"), Output("holding-table", "data"), Output("sync-info", "children"),
              Output("backup-info", "children"), Input("version", "data"))
def data_tab(version):
    if STATE["error"]:
        return no_data_notice(), [], "", ""
    d = STATE["data"]
    label = {"seeded": "copied from an old spreadsheet", "epos_export": "Epos Now export", "epos_api": "Epos Now (direct)"}
    terms = [{"Term": f.qlabel(k), "Source": label.get(t["source"], t["source"]), "Units": t["units"],
              "Loaded": (t["imported_at"] or "")[:16].replace("T", " "), "File": t["file_name"] or ""} for k, t in sorted(d.terms.items())][::-1]
    snaps = [{"Taken": s["taken_at"].replace("T", " "), "Products": len(s["levels"]),
              "Confirmed": ("yes" + (f" ({s['confirmed_by']})" if s["confirmed_by"] else "")) if s["confirmed"] else "no"}
             for s in d.snapshots][::-1]
    tbl = lambda rows, page=6: dash_table.DataTable(data=rows, columns=[{"name": c, "id": c} for c in rows[0]] if rows else [], page_size=page, **TABLE_STYLE)
    shown = set(eng.run(d).items["name"])
    neg = [{"Product": n, "Stock in Epos Now": v, "In the order list": "yes" if n in shown else "no"} for n, v in sorted(d.negatives.items())]
    body = [*freshness_notes(d, eng.run(d)),
            html.Div(className="row2", children=[
                html.Div([html.H3("Sales by term", style={"fontSize": "14px"}), tbl(terms, 6)]),
                html.Div([html.H3("Stock reports", style={"fontSize": "14px"}), tbl(snaps, 6),
                          html.Details([html.Summary(f"{len(neg)} product(s) with negative stock in the newest report"), tbl(neg, 10)]) if neg else None])])]
    body += [html.Div(w, className="warn") for w in d.load_warnings]
    try:
        import sync
        info = sync.describe()
    except Exception as e:                                   # sync is optional
        info = f"Epos Now sync is not available: {e}"
    cfg_dir = d.cfg["settings"].get("backup_dir", "")
    where = cfg_dir or "output/backup (inside the program folder)"
    binfo = [f"After every load the program writes a full CSV copy of the database to: {where}. ",
             "" if cfg_dir else html.B("Set backup_dir in config.toml to a folder that syncs to the shared folder (SharePoint, OneDrive, Dropbox...) so the data survives the loss of this computer. "),
             "The zip below holds the same files. sales_history.csv is the main one."]
    return body, store_holdings(), info, binfo


def store_holdings():
    c = db()
    try:
        return store.get_holdings(c)
    finally:
        c.close()


@app.callback(Output("up-sales-msg", "children"), Output("version", "data", allow_duplicate=True),
              Input("up-sales", "contents"), State("up-sales", "filename"), State("up-year", "value"),
              State("up-term", "value"), State("version", "data"), prevent_initial_call=True)
def upload_sales(contents, filename, year, term, version):
    if not contents:
        return no_update, no_update
    c = db()
    try:
        res = store.import_sales(c, _decode(contents), int(year), int(term), file_name=filename or "", root=eng.ROOT)
    except (store.StoreError, ValueError, TypeError) as e:
        return _fail(e), no_update
    finally:
        c.close()
    return _msg(res, "warn" if res["status"] in ("unchanged", "skipped") else "ok"), _bump(version)


@app.callback(Output("up-stock-msg", "children"), Output("version", "data", allow_duplicate=True),
              Input("up-stock", "contents"), State("up-stock", "filename"), State("up-confirmed", "value"),
              State("up-confirmed-by", "value"), State("version", "data"), prevent_initial_call=True)
def upload_stock(contents, filename, confirmed, by, version):
    if not contents:
        return no_update, no_update
    c = db()
    try:
        res = store.import_stock(c, _decode(contents), file_name=filename or "", confirmed=bool(confirmed), confirmed_by=by or "", root=eng.ROOT)
    except (store.StoreError, ValueError, TypeError) as e:
        return _fail(e), no_update
    finally:
        c.close()
    return _msg(res, "warn" if res["status"] == "unchanged" else "ok"), _bump(version)


@app.callback(Output("up-stock-msg", "children", allow_duplicate=True), Output("version", "data", allow_duplicate=True),
              Input("btn-confirm", "n_clicks"), State("up-confirmed-by", "value"), State("version", "data"), prevent_initial_call=True)
def confirm_latest(n, by, version):
    d = STATE["data"]
    c = db()
    try:
        store.confirm_snapshot(c, d.latest["snapshot_id"], by or "")
    finally:
        c.close()
    return html.Div(f"The stock report from {d.stock_taken:%d %b %Y} is now marked as confirmed.", className="ok"), _bump(version)


@app.callback(Output("holding-table", "data", allow_duplicate=True), Input("hold-add", "n_clicks"),
              State("holding-table", "data"), prevent_initial_call=True)
def add_hold_row(n, rows):
    return (rows or []) + [{"Name": "", "Qty": 0}]


@app.callback(Output("hold-msg", "children"), Output("version", "data", allow_duplicate=True),
              Input("hold-save", "n_clicks"), State("holding-table", "data"), State("version", "data"), prevent_initial_call=True)
def save_holding(n, rows, version):
    c = db()
    try:
        res = store.set_holdings(c, [(r["Name"], int(float(r.get("Qty") or 0))) for r in (rows or []) if str(r.get("Name", "")).strip()], root=eng.ROOT)
    except (store.StoreError, ValueError) as e:
        return _fail(e), no_update
    finally:
        c.close()
    return _msg(res), _bump(version)


@app.callback(Output("sync-msg", "children"), Output("version", "data", allow_duplicate=True),
              Input("sync-sales", "n_clicks"), Input("sync-stock", "n_clicks"),
              State("sync-year", "value"), State("sync-term", "value"), State("version", "data"), prevent_initial_call=True)
def run_sync(n1, n2, year, term, version):
    try:
        import sync
        msg = sync.pull_sales(int(year), int(term)) if ctx.triggered_id == "sync-sales" else sync.pull_stock()
    except Exception as e:
        return html.Div(f"Sync failed: {e}", className="err"), no_update
    return html.Div(msg, className="ok"), _bump(version)


@app.callback(Output("dl-backup", "data"), Input("btn-backup", "n_clicks"), prevent_initial_call=True)
def download_backup(n):
    c = db()
    try:
        data = store.export_zip_bytes(c)
    finally:
        c.close()
    return dcc.send_bytes(data, f"stock_planner_data_{datetime.now():%Y-%m-%d}.zip")


@app.callback(Output("backup-msg", "children"), Input("btn-backup-now", "n_clicks"), prevent_initial_call=True)
def backup_now(n):
    c = db()
    try:
        out = store.run_backup(c, eng.ROOT)
    finally:
        c.close()
    return html.Div(out, className="warn" if out.startswith("WARNING") else "ok")


# ----------------------------------------------------------------------------- tab: context
@app.callback(Output("ctx-enrol", "figure"), Output("ctx-per100", "figure"), Input("ctx-line", "value"), *SCEN)
def ctx_charts(line, version, buffer, onorder, dq_vals, dq_ids):
    if STATE["error"]:
        e = empty_fig("No data")
        return e, e
    d = STATE["data"]
    grid = d.enrolment_grid
    if not grid or len(grid[0]) < 2:
        e = empty_fig("No enrolments entered yet - add them below.")
        return e, e
    years = [k for k in grid[0] if k != "Year level"]
    fig = go.Figure()
    palette = ["#1d4ed8", "#2563eb", "#3b82f6", "#60a5fa", "#0d9488", "#14b8a6", "#5eead4"]
    levels = [r for r in grid if r["Year level"] != "Total"]
    has_levels = {y: any(r[y] is not None for r in levels) for y in years}
    for i, r in enumerate(levels):
        fig.add_bar(x=years, y=[r[y] for y in years], name=("Prep" if r["Year level"] == "Prep" else f"Year {r['Year level']}"), marker_color=palette[i % len(palette)])
    tot = next(r for r in grid if r["Year level"] == "Total")
    fig.add_bar(x=years, y=[None if has_levels[y] else tot[y] for y in years], name="Total only (no year levels)", marker_color=GREY)
    fig.add_trace(go.Scatter(x=years, y=[tot[y] for y in years], mode="text", text=[f"{tot[y]:,}" if tot[y] else "" for y in years],
                             textposition="top center", showlegend=False, hoverinfo="skip"))
    fig.update_layout(barmode="stack")
    fig.update_yaxes(title="Students (August census)")
    style_fig(fig, "Enrolments by year level", h=420)

    res = scenario(version, buffer, onorder, dq_vals, dq_ids)
    t = eng.per_100_students(res, d, line)
    if t.empty or t["per100"].isna().all():
        p = empty_fig("Needs enrolments for the years this line has sales")
    else:
        p = go.Figure(go.Bar(x=t["period"], y=t["per100"], marker_color=BLUE, name="Units per 100 students",
                             text=[f"{v:.0f}" if v == v and v is not None else "" for v in t["per100"]], textposition="outside"))
        p.add_trace(go.Scatter(x=t["period"], y=t["students"], name="Students (right axis)", yaxis="y2", mode="lines+markers",
                               line=dict(color=ORANGE)))
        p.update_layout(yaxis2=dict(overlaying="y", side="right", title="Students", showgrid=False, rangemode="tozero"))
        p.update_layout(yaxis_title="Units sold per 100 students")
    style_fig(p, f"{line}: units sold per 100 students", h=400)
    return fig, p


@app.callback(Output("enrol-table", "data"), Output("enrol-table", "columns"), Output("notes-table", "data"), Input("version", "data"))
def ctx_tables(version):
    if STATE["error"]:
        return [], [], []
    d = STATE["data"]
    grid = d.enrolment_grid or [{"Year level": l} for l in store.YEAR_LEVELS + ["Total"]]
    cols = [{"name": c, "id": c, "editable": c != "Year level", "type": "numeric" if c != "Year level" else "text"} for c in grid[0]]
    notes = [{"ID": n["annotation_id"], "Year": n["year"], "Term": f"Term {n['term']}" if n["term"] else "Whole year", "Note": n["note"]} for n in d.annotations]
    return grid, cols, notes


@app.callback(Output("enrol-table", "columns", allow_duplicate=True), Output("enrol-table", "data", allow_duplicate=True),
              Input("enrol-add", "n_clicks"), State("enrol-new-year", "value"), State("enrol-table", "columns"), State("enrol-table", "data"),
              prevent_initial_call=True)
def add_year(n, year, cols, data):
    if not year or str(int(year)) in [c["id"] for c in cols]:
        return no_update, no_update
    key = str(int(year))
    cols = cols + [{"name": key, "id": key, "editable": True, "type": "numeric"}]
    for r in data:
        r[key] = None
    return cols, data


@app.callback(Output("enrol-msg", "children"), Output("version", "data", allow_duplicate=True), Input("enrol-save", "n_clicks"),
              State("enrol-table", "data"), State("version", "data"), prevent_initial_call=True)
def save_enrolments(n, data, version):
    years = [k for k in (data[0] if data else {}) if k != "Year level"]
    rows = []
    for y in years:
        by = {r["Year level"]: r.get(y) for r in data}
        levels_given = any(by.get(l) not in (None, "") for l in store.YEAR_LEVELS)
        for l in store.YEAR_LEVELS:
            rows.append({"year": y, "year_level": l, "students": by.get(l)})
        if not levels_given:                       # only keep a typed Total when there is no year-level split
            rows.append({"year": y, "year_level": "Total", "students": by.get("Total")})
    c = db()
    try:
        res = store.set_enrolments(c, rows, root=eng.ROOT)
    except (store.StoreError, ValueError) as e:
        return _fail(e), no_update
    finally:
        c.close()
    return _msg(res), _bump(version)


@app.callback(Output("notes-msg", "children"), Output("version", "data", allow_duplicate=True), Input("note-add", "n_clicks"),
              State("note-year", "value"), State("note-term", "value"), State("note-text", "value"), State("version", "data"), prevent_initial_call=True)
def add_note(n, year, term, text, version):
    c = db()
    try:
        store.add_annotation(c, int(year), int(term or 0) or None, text or "", root=eng.ROOT)
    except (store.StoreError, ValueError, TypeError) as e:
        return _fail(e), no_update
    finally:
        c.close()
    return html.Div("Note added.", className="ok"), _bump(version)


@app.callback(Output("notes-msg", "children", allow_duplicate=True), Output("version", "data", allow_duplicate=True), Input("notes-save", "n_clicks"),
              State("notes-table", "data"), State("version", "data"), prevent_initial_call=True)
def save_notes(n, rows, version):
    keep = {int(r["ID"]) for r in (rows or [])}
    c = db()
    try:
        gone = [n_["annotation_id"] for n_ in store.get_annotations(c) if n_["annotation_id"] not in keep]
        for i in gone:
            store.delete_annotation(c, i)
        store.run_backup(c, eng.ROOT)
    finally:
        c.close()
    return html.Div(f"Removed {len(gone)} note(s).", className="ok"), _bump(version)


# ----------------------------------------------------------------------------- run
def main():
    ap = argparse.ArgumentParser(description="Stock Planner dashboard")
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--webhook", action="store_true", help="also listen for Epos Now webhooks (see README)")
    a = ap.parse_args()
    if a.webhook:
        import sync
        sync.register_webhook(server)
    url = f"http://127.0.0.1:{a.port}"
    if not a.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"Dashboard running at {url}  (press Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=a.port, debug=a.debug)


if __name__ == "__main__":
    main()
