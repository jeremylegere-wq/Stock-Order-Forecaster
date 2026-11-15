#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
Epos Now connection: pull sales and stock straight from Epos Now instead of exporting CSVs.

    python sync.py probe              # check the key works and show what the API returns
    python sync.py sales 2027Q1       # write data/sales/2027Q1.csv for that term
    python sync.py stock              # write a dated stock report into data/stock
    python sync.py webhook-info       # show how to set up the optional webhook receiver

Credentials are read from a file called .env in this folder (never from the code):

    EPOS_API_KEY=...
    EPOS_API_SECRET=...

Two ways in, and which to prefer:
  * PULL (recommended): this program asks Epos Now for the data whenever you press a button.
    Works from a laptop, nothing needs to be exposed to the internet.
  * WEBHOOK (optional): Epos Now POSTs each completed sale to a web address. Epos Now's servers
    must be able to reach that address, so a laptop needs a tunnel (e.g. cloudflared/ngrok), and
    sales made while the laptop is off are missed unless resent from the Back Office Webhook Queue.
    The receiver below logs the events; the pull remains the source of truth.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import forecast as f
import store

try:
    import tomllib
except ModuleNotFoundError:           # Python < 3.11
    import tomli as tomllib           # type: ignore

ROOT = f.ROOT
TERM_DATES = {1: ((1, 1), (3, 31)), 2: ((4, 1), (6, 30)), 3: ((7, 1), (9, 30)), 4: ((10, 1), (12, 31))}


class SyncError(Exception):
    pass


# --------------------------------------------------------------------------- settings
def load_env(path: Path = ROOT / ".env") -> dict:
    env = dict(os.environ)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


def load_cfg(path: Path = ROOT / "sync.toml") -> dict:
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def is_configured(env: dict | None = None) -> bool:
    env = env or load_env()
    return bool(env.get("EPOS_API_KEY") and env.get("EPOS_API_SECRET"))


def describe() -> str:
    if is_configured():
        return "Epos Now key found in .env. Use the buttons to pull a term's sales or today's stock."
    return ("Not connected yet. Add EPOS_API_KEY and EPOS_API_SECRET to a file called .env in the app folder "
            "(see README, section 'Connecting to Epos Now'), then restart the dashboard.")


def term_range(year: int, term: int) -> tuple[date, date]:
    (m1, d1), (m2, d2) = TERM_DATES[term]
    return date(year, m1, d1), date(year, m2, d2)


# --------------------------------------------------------------------------- HTTP
class Client:
    def __init__(self, cfg: dict, env: dict):
        if not is_configured(env):
            raise SyncError("No Epos Now key found. Add EPOS_API_KEY and EPOS_API_SECRET to the .env file.")
        token = base64.b64encode(f"{env['EPOS_API_KEY']}:{env['EPOS_API_SECRET']}".encode()).decode()
        self.base = cfg["api"]["base_url"].rstrip("/")
        self.size = int(cfg["api"].get("page_size", 200))
        self.headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json", "Accept": "application/json"}

    def get(self, path: str, params: dict | None = None):
        url = f"{self.base}/{path.strip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        req = urllib.request.Request(url, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8") or "[]")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise SyncError("Epos Now rejected the key/secret (check .env, and that the API device is enabled).") from None
            if e.code == 429:
                raise SyncError("Epos Now says too many requests - wait a minute and try again.") from None
            raise SyncError(f"Epos Now returned HTTP {e.code} for {path}.") from None
        except urllib.error.URLError as e:
            raise SyncError(f"Could not reach Epos Now ({e.reason}). Check the internet connection and base_url in sync.toml.") from None

    def pages(self, path: str, params: dict | None = None, max_pages: int = 5000):
        seen_first = None
        for page in range(1, max_pages + 1):
            data = as_list(self.get(path, {**(params or {}), "page": page}))
            if not data:
                return
            marker = json.dumps(data[0], sort_keys=True)
            if marker == seen_first:                       # server ignored the page number
                return
            seen_first = marker if page == 1 else seen_first
            yield from data
            if len(data) < self.size:
                return


def as_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for v in payload.values():
            if isinstance(v, list):
                return v
    return []


def parse_dt(s) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


# --------------------------------------------------------------------------- pulls
def product_names(client: Client, cfg: dict) -> dict:
    c = cfg["products"]
    return {str(p.get(c["id_field"])): p for p in client.pages(c["path"])}


def pull_sales(year: int, term: int, client: Client | None = None, cfg: dict | None = None, today: date | None = None) -> str:
    cfg = cfg or load_cfg()
    client = client or Client(cfg, load_env())
    start, end = term_range(year, term)
    t, p = cfg["transactions"], cfg["products"]
    params = {t.get("from_param"): f"{start:%Y-%m-%d} 00:00:00", t.get("to_param"): f"{end:%Y-%m-%d} 23:59:59"}
    params.pop("", None)
    prods = product_names(client, cfg)
    totals: dict[str, int] = defaultdict(int)
    n_tx = n_skipped = 0
    for tx in client.pages(t["path"], params):
        when = parse_dt(tx.get(t["date_field"]))
        if when is not None and not (start <= when.date() <= end):
            continue                                      # also filter here, in case the server ignored the dates
        if t.get("status_field") and tx.get(t["status_field"]) in t.get("skip_status", []):
            n_skipped += 1
            continue
        n_tx += 1
        for it in tx.get(t["items_field"]) or []:
            prod = prods.get(str(it.get(t["product_id_field"])))
            name = (prod or {}).get(p["name_field"]) or it.get("Name") or it.get("Description")
            if not name:
                continue
            totals[str(name).strip()] += int(round(float(it.get(t["qty_field"]) or 0)))
    if not totals:
        raise SyncError(f"Epos Now returned no sales for {year} Term {term} ({start:%d %b} to {end:%d %b}).")
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Name", "Qty"])
    for name in sorted(totals):
        w.writerow([name, totals[name]])
    conn = store.ensure_ready(ROOT)
    try:
        res = store.import_sales(conn, buf.getvalue().encode("utf-8"), year, term, source="epos_api",
                                 file_name=f"Epos Now API {datetime.now():%Y-%m-%d %H:%M}", root=ROOT)
    except store.StoreError as e:
        raise SyncError(str(e)) from None
    finally:
        conn.close()
    today = today or date.today()
    open_note = ""
    if today <= end:
        open_note = f" Term {term} has not finished yet, so it will be treated as a partial term until {end:%d %b %Y}."
    skip_note = f" {n_skipped} voided transactions skipped." if n_skipped else ""
    return f"{res['message']} ({n_tx} transactions read from Epos Now.){skip_note}{open_note}"


def pull_stock(client: Client | None = None, cfg: dict | None = None) -> str:
    cfg = cfg or load_cfg()
    client = client or Client(cfg, load_env())
    s, p = cfg["stock"], cfg["products"]
    prods = product_names(client, cfg)
    cur: dict[str, float] = defaultdict(float)
    oo: dict[str, float] = defaultdict(float)
    for r in client.pages(s["path"]):
        pid = str(r.get(s["product_id_field"]))
        cur[pid] += float(r.get(s["current_field"]) or 0)          # several locations add up
        oo[pid] += float(r.get(s["on_order_field"]) or 0)
    if not cur:
        raise SyncError("Epos Now returned no stock levels.")
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Name", "TotalStock", "MeasuredOnOrder", "MeasuredCostPrice"])
    n = 0
    for pid, qty in cur.items():
        prod = prods.get(pid)
        if not prod or not prod.get(p["name_field"]):
            continue
        w.writerow([prod[p["name_field"]], qty, oo[pid], prod.get(p.get("cost_field", "CostPrice")) or 0])
        n += 1
    conn = store.ensure_ready(ROOT)
    try:
        res = store.import_stock(conn, buf.getvalue().encode("utf-8"), file_name=f"Epos Now API {datetime.now():%Y-%m-%d %H:%M}",
                                 taken_at=datetime.now().isoformat(timespec="minutes"), source="epos_api", root=ROOT)
    except store.StoreError as e:
        raise SyncError(str(e)) from None
    finally:
        conn.close()
    return res["message"]


def probe() -> None:
    cfg = load_cfg()
    client = Client(cfg, load_env())
    for section in ("transactions", "products", "stock"):
        path = cfg[section]["path"]
        print(f"\n--- {section}: GET {client.base}/{path}?page=1")
        data = as_list(client.get(path, {"page": 1}))
        print(f"{len(data)} record(s) on page 1")
        if data:
            print("fields:", ", ".join(sorted(data[0].keys())))
            print(json.dumps(data[0], indent=2, default=str)[:1500])
    print("\nCompare the field names above with sync.toml and change any that differ.")


# --------------------------------------------------------------------------- webhook (optional)
EVENTS = ROOT / "data" / "events"


def register_webhook(server) -> None:
    """Adds POST /epos/webhook to the dashboard's web server. Requires WEBHOOK_USER/WEBHOOK_PASS in .env."""
    from flask import request

    env = load_env()
    user, pw = env.get("EPOS_WEBHOOK_USER"), env.get("EPOS_WEBHOOK_PASS")
    if not (user and pw):
        raise SyncError("Set EPOS_WEBHOOK_USER and EPOS_WEBHOOK_PASS in .env first. The receiver refuses to run without a password.")
    expected = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()

    @server.route("/epos/webhook", methods=["POST"])
    def epos_webhook():
        import hmac
        if not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
            return ("unauthorised", 401)
        EVENTS.mkdir(parents=True, exist_ok=True)
        body = request.get_data(as_text=True)[:200_000]
        rec = {"received": datetime.now().isoformat(timespec="seconds"),
               "object": request.headers.get("Epos-Object"), "action": request.headers.get("Epos-Action"), "body": body}
        with open(EVENTS / "webhook_events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        return ("", 204)

    print("Webhook receiver listening at /epos/webhook (events are logged to data/events/webhook_events.jsonl)")


def webhook_info() -> None:
    print(__doc__.split("Two ways in")[1])
    log = EVENTS / "webhook_events.jsonl"
    if log.exists():
        lines = log.read_text(encoding="utf-8").splitlines()
        print(f"\n{len(lines)} event(s) logged; last at {json.loads(lines[-1])['received']}")


def main(argv=None) -> None:
    a = list(sys.argv[1:] if argv is None else argv)
    try:
        if a[:1] == ["probe"]:
            probe()
        elif a[:1] == ["sales"] and len(a) == 2:
            m = f.QFILE.search(a[1])
            if not m:
                raise SyncError("Say which term, e.g.  python sync.py sales 2027Q1")
            print(pull_sales(int(m.group(1)), int(m.group(2))))
        elif a[:1] == ["stock"]:
            print(pull_stock())
        elif a[:1] == ["webhook-info"]:
            webhook_info()
        else:
            print(__doc__)
    except SyncError as e:
        sys.exit(f"Sync problem: {e}")


if __name__ == "__main__":
    main()
