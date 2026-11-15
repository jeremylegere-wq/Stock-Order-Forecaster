#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end smoke test on a throw-away copy of the project (standard library only).

Runs the command-line forecast on the bundled demo data, checks the database CLI, then starts the
dashboard and asks it for its page and layout. Exits non-zero with a clear message if any step fails.
Run from anywhere:   python scripts/smoke_test.py
"""
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT_S = 90


def run(step: str, cwd: Path, *args: str) -> str:
    print(f"== {step}", flush=True)
    p = subprocess.run([sys.executable, *args], cwd=cwd, capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        sys.exit(f"FAILED: {step}\n--- stdout ---\n{p.stdout}\n--- stderr ---\n{p.stderr}")
    return p.stdout


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fetch(url: str) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read().decode("utf-8", "replace")


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="smoke_")) / "project"
    shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(".git", ".venv", "output", "__pycache__", "*.db"))
    try:
        out = run("forecast on demo data", work, "forecast.py", "--only-orders")
        if "items need an order" not in out:
            sys.exit(f"FAILED: forecast printed no order summary:\n{out}")
        out = run("database status", work, "store.py", "status")
        if "terms" not in out.lower():
            sys.exit(f"FAILED: unexpected store status output:\n{out}")
        if not (work / "output" / "stock_forecast_latest.xlsx").exists():
            sys.exit("FAILED: the Excel workbook was not written")

        port = free_port()
        print(f"== dashboard on port {port}", flush=True)
        srv = subprocess.Popen([sys.executable, "dashboard.py", "--no-browser", "--port", str(port)], cwd=work,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            deadline, last = time.time() + TIMEOUT_S, None
            while time.time() < deadline:
                if srv.poll() is not None:
                    sys.exit(f"FAILED: dashboard exited early (code {srv.returncode}):\n{srv.stdout.read()}")
                try:
                    status, _ = fetch(f"http://127.0.0.1:{port}/")
                    status2, layout = fetch(f"http://127.0.0.1:{port}/_dash-layout")
                    if status == 200 and status2 == 200 and "Order sheet" in layout:
                        print("dashboard OK")
                        break
                except OSError as e:
                    last = e
                time.sleep(1)
            else:
                sys.exit(f"FAILED: dashboard did not answer within {TIMEOUT_S}s ({last})")
        finally:
            srv.terminate()
            try:
                srv.wait(10)
            except subprocess.TimeoutExpired:
                srv.kill()
    finally:
        shutil.rmtree(work.parent, ignore_errors=True)
    print("smoke test passed")


if __name__ == "__main__":
    main()
