"""Run the full pipeline: ingest -> clean -> aggregate -> build_site.

Logs every run to reports/run_log.txt. Before rebuilding the site, compares
the new total active-vehicle count against the last successful run's count;
if it moved by more than SANITY_CHANGE_THRESHOLD, stops and prints a warning
instead of publishing a site built on (possibly) broken data - per the
project brief.

Incremental API fetch: by default, continues from the end date of the last
successful API ingest_log entry (or the last INITIAL_BOOTSTRAP_DAYS days, if
there's no prior successful run) up to yesterday. A full historical backfill
back to the bulk file's 2022-04-17 snapshot is NOT done automatically here -
see NOTES.md on why that's a multi-hour, rate-limited job better run
deliberately on its own (`python ingest.py --api-from ... --api-to ...`),
not on every pipeline run.
"""
from __future__ import annotations

import re
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "cepik.duckdb"
REPORTS_DIR = BASE_DIR / "reports"
RUN_LOG_PATH = REPORTS_DIR / "run_log.txt"

SANITY_CHANGE_THRESHOLD = 0.20  # ±20%
INITIAL_BOOTSTRAP_DAYS = 14


def log(message: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {message}"
    print(line)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with RUN_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run_step(args: list[str]) -> None:
    log(f"running: {' '.join(args)}")
    result = subprocess.run([sys.executable, *args], cwd=BASE_DIR)
    if result.returncode != 0:
        log(f"FAILED: {' '.join(args)} exited with code {result.returncode}")
        sys.exit(result.returncode)


def determine_api_range(con: duckdb.DuckDBPyConnection) -> tuple[date, date]:
    rows = con.execute(
        "SELECT params FROM ingest_log WHERE source = 'api' AND status = 'success'"
    ).fetchall()
    last_to = None
    for (params,) in rows:
        m = re.search(r"to=(\d{4}-\d{2}-\d{2})", params)
        if m:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            if last_to is None or d > last_to:
                last_to = d

    yesterday = date.today() - timedelta(days=1)
    if last_to is None:
        return yesterday - timedelta(days=INITIAL_BOOTSTRAP_DAYS), yesterday
    return last_to + timedelta(days=1), yesterday


def get_total_active(con: duckdb.DuckDBPyConnection) -> int | None:
    try:
        return con.execute("SELECT SUM(n_vehicles) FROM agg_vehicle_counts").fetchone()[0]
    except duckdb.Error:
        return None


def main() -> None:
    log("=== run_all.py starting ===")

    con = duckdb.connect(str(DB_PATH))
    previous_total = get_total_active(con)
    api_from, api_to = determine_api_range(con)
    con.close()

    log(f"previous total active vehicles: {previous_total}")
    log(f"incremental API window: {api_from} .. {api_to}")

    run_step(["ingest.py", "--bulk"])
    if api_from <= api_to:
        run_step(["ingest.py", "--api-from", str(api_from), "--api-to", str(api_to)])
    else:
        log("API window empty (already up to date) - skipping")

    run_step(["clean.py"])
    run_step(["aggregate.py"])

    con = duckdb.connect(str(DB_PATH), read_only=True)
    new_total = get_total_active(con)
    con.close()

    log(f"new total active vehicles: {new_total}")

    if previous_total and new_total:
        change = abs(new_total - previous_total) / previous_total
        if change > SANITY_CHANGE_THRESHOLD:
            log(
                f"WARNING: active vehicle count changed by {change:.1%} "
                f"({previous_total:,} -> {new_total:,}), exceeding the "
                f"{SANITY_CHANGE_THRESHOLD:.0%} sanity threshold. Stopping "
                "WITHOUT rebuilding the site - investigate before re-running."
            )
            sys.exit(1)

    run_step(["build_site.py"])
    log("=== run_all.py finished successfully ===")


if __name__ == "__main__":
    main()
