"""
test_live.py — Live pipeline monitor & integration test
========================================================
Connects to the real Smart i-Lab REST API and exercises the full
Bronze layer pipeline against the real smart_ilab.duckdb.

What it does each cycle
-----------------------
  1. DB state snapshot  — show table existence, row count, latest timestamp
  2. API timestamp check — fetch the latest reading timestamp from the API
  3. Gap calculation    — how far behind is the DB?
  4. Ingest (if stale)  — fetch new rows, write Parquet, upsert bronze table
  5. Post-ingest diff   — show exactly how many rows were added and new latest ts

Usage
-----
  # One-shot: check + ingest one cycle for all device types
  python test_live.py --all

  # Poll every 60 s for air-1 only (Ctrl+C to stop)
  python test_live.py --device-type air-1 --poll 60

  # Bootstrap from last 48 h, then poll every 2 min for all types
  python test_live.py --all --lookback 48 --poll 120

  # Read-only: just show DB vs API state, never write anything
  python test_live.py --all --check-only

  # Force a full rebuild of the bronze table this cycle
  python test_live.py --device-type air-1 --force-rebuild
"""

import argparse
import importlib
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd

# ── Load pipeline modules from the project directory ─────────────────────────
_storage = importlib.import_module("CSV Training Data Code")
_api     = importlib.import_module("api_ingestion")

SmartILabAPIClient    = _api.SmartILabAPIClient
check_needs_update    = _api.check_needs_update
ingest_and_rebuild_bronze = _api.ingest_and_rebuild_bronze

DEVICE_TYPES                = _storage.DEVICE_TYPES
get_latest_stored_timestamp = _storage.get_latest_stored_timestamp
_db                         = _storage._db
_q                          = _storage._q
_bronze_table               = _storage._bronze_table
_silver_table               = _storage._silver_table
_table_exists               = _storage._table_exists

BASE_URL = _api.BASE_URL
API_KEY  = _api.API_KEY

# ── Terminal colour helpers (no external library) ─────────────────────────────
_USE_COLOUR = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOUR else text

def _green(t):   return _c("32",   t)
def _yellow(t):  return _c("33",   t)
def _red(t):     return _c("31",   t)
def _cyan(t):    return _c("36",   t)
def _bold(t):    return _c("1",    t)
def _dim(t):     return _c("2",    t)

LINE = "─" * 64
DLINE = "═" * 64


# =============================================================================
# Formatting helpers
# =============================================================================

def _ts(dt) -> str:
    """Format a datetime for display; handle None gracefully."""
    if dt is None:
        return _dim("(none)")
    if hasattr(dt, "replace"):
        return dt.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    return str(dt)


def _lag(db_ts, api_ts) -> str:
    """Human-readable lag between DB and API timestamp."""
    if db_ts is None or api_ts is None:
        return ""
    secs = int((api_ts - db_ts).total_seconds())
    if secs < 0:
        return _green("DB ahead?")
    if secs < 60:
        return f"{secs}s behind"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s behind"
    return f"{secs // 3600}h {(secs % 3600) // 60}m behind"


def _row_count(table: str) -> int:
    if not _table_exists(table):
        return 0
    return _db.execute(f'SELECT COUNT(*) FROM {_q(table)}').fetchone()[0]


# =============================================================================
# Per-device cycle
# =============================================================================

def _run_device_cycle(
    device_type:   str,
    client:        SmartILabAPIClient,
    cycle:         int,
    lookback_hours: int,
    check_only:    bool,
    force_rebuild: bool,
) -> dict:
    """
    Execute one full check+ingest cycle for a single device type.
    Returns a result dict for the summary table.
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bronze  = _bronze_table(device_type)

    print()
    print(DLINE)
    print(_bold(f"[{now_str}]  Cycle #{cycle}  │  {device_type}"))
    print(DLINE)

    # ── Step 1: DB state ──────────────────────────────────────────────────────
    table_exists = _table_exists(bronze)
    rows_before  = _row_count(bronze)
    db_ts        = get_latest_stored_timestamp(device_type, layer="bronze")

    print(f"  {'Bronze table':<16}  {_cyan(bronze)}")
    if table_exists:
        print(f"  {'Exists':<16}  {_green('YES')}  ({rows_before:,} rows)")
        print(f"  {'DB latest':<16}  {_ts(db_ts)}")
    else:
        print(f"  {'Exists':<16}  {_yellow('NO')}  (will bootstrap)")

    # ── Step 2: API timestamp check ───────────────────────────────────────────
    print(f"\n  Checking API …")
    t_api_start = time.perf_counter()
    api_ts      = client.get_latest_api_timestamp(device_type)
    api_latency = time.perf_counter() - t_api_start

    if api_ts is None:
        print(f"  {'API latest':<16}  {_red('UNREACHABLE')}  (skipping)")
        return {"device": device_type, "status": "API_UNREACHABLE", "inserted": 0,
                "rows_after": rows_before, "lag": None}

    print(f"  {'API latest':<16}  {_ts(api_ts)}  {_dim(f'({api_latency*1000:.0f} ms)')}")

    lag_str = _lag(db_ts, api_ts)
    if db_ts:
        needs_update = api_ts.replace(microsecond=0) > db_ts.replace(microsecond=0)
        if needs_update:
            print(f"  {'Gap':<16}  {_yellow(lag_str)}")
        else:
            print(f"  {'Status':<16}  {_green('UP TO DATE')}")
    else:
        needs_update = True
        print(f"  {'Status':<16}  {_yellow('NEEDS BOOTSTRAP')}")

    if not needs_update and not force_rebuild:
        print(LINE)
        return {"device": device_type, "status": "UP_TO_DATE", "inserted": 0,
                "rows_after": rows_before, "lag": lag_str}

    if check_only:
        print(f"  {_dim('(--check-only: skipping ingest)')}")
        print(LINE)
        return {"device": device_type, "status": "STALE_SKIPPED", "inserted": 0,
                "rows_after": rows_before, "lag": lag_str}

    # ── Step 3: Ingest ────────────────────────────────────────────────────────
    action = "Force-rebuilding" if force_rebuild else "Ingesting new data"
    print(f"\n  {_bold(action)} …")

    t_ingest_start = time.perf_counter()
    try:
        parquet_rows, _ = ingest_and_rebuild_bronze(
            device_type=device_type,
            client=client,
            lookback_hours=lookback_hours,
            flush_force=True,
        )
        ingest_elapsed = time.perf_counter() - t_ingest_start
    except Exception as exc:
        print(f"  {_red(f'INGEST ERROR: {exc}')}")
        return {"device": device_type, "status": "ERROR", "inserted": 0,
                "rows_after": rows_before, "lag": lag_str}

    # ── Step 4: Post-ingest snapshot ──────────────────────────────────────────
    rows_after = _row_count(bronze)
    new_db_ts  = get_latest_stored_timestamp(device_type, layer="bronze")
    inserted   = max(0, rows_after - rows_before)

    print()
    print(f"  {'Parquet rows':<16}  {parquet_rows:,} written")
    print(f"  {'Bronze delta':<16}  {_green(f'+{inserted:,}')} rows")
    print(f"  {'Bronze total':<16}  {rows_after:,} rows")
    print(f"  {'DB latest now':<16}  {_ts(new_db_ts)}")
    if new_db_ts and api_ts:
        still_behind = (api_ts.replace(microsecond=0) - new_db_ts.replace(microsecond=0)).total_seconds()
        if still_behind <= 0:
            print(f"  {'Sync':<16}  {_green('✓ Fully caught up')}")
        else:
            print(f"  {'Sync':<16}  {_yellow(f'Still {int(still_behind)}s behind (API may have moved)')}")
    print(f"  {_dim(f'Ingest took {ingest_elapsed:.1f}s')}")
    print(LINE)

    return {"device": device_type, "status": "INGESTED", "inserted": inserted,
            "rows_after": rows_after, "lag": lag_str}


# =============================================================================
# Summary table after each full cycle across all device types
# =============================================================================

def _print_summary(results: list[dict], cycle: int, elapsed: float):
    print()
    print(_bold(f"Cycle #{cycle} summary  ({elapsed:.1f}s total)"))
    print(f"  {'Device':<20}  {'Status':<16}  {'Inserted':>10}  {'Total rows':>12}")
    print(f"  {'-'*20}  {'-'*16}  {'-'*10}  {'-'*12}")
    for r in results:
        status_str = {
            "UP_TO_DATE":    _green("OK"),
            "INGESTED":      _green("INGESTED"),
            "STALE_SKIPPED": _yellow("STALE/SKIPPED"),
            "API_UNREACHABLE": _red("UNREACHABLE"),
            "ERROR":         _red("ERROR"),
        }.get(r["status"], r["status"])
        ins_str = _green(f"+{r['inserted']:,}") if r["inserted"] > 0 else _dim("  —")
        print(f"  {r['device']:<20}  {status_str:<25}  {ins_str:>10}  {r['rows_after']:>12,}")
    print()


# =============================================================================
# Entry point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Live BSG pipeline monitor — real API, real DuckDB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--device-type", default=None, choices=DEVICE_TYPES,
                        help="Single device type (default: air-1 unless --all)")
    parser.add_argument("--all",          action="store_true",
                        help="Run all 6 device types per cycle")
    parser.add_argument("--lookback",     type=int, default=24,
                        help="Bootstrap lookback hours if DB is empty (default: 24)")
    parser.add_argument("--poll",         type=int, default=0,
                        help="Poll interval in seconds; 0 = one-shot (default: 0)")
    parser.add_argument("--check-only",   action="store_true",
                        help="Show DB vs API state only, never write to DB")
    parser.add_argument("--force-rebuild", action="store_true",
                        help="Rebuild bronze tables from Parquet even if up to date")
    args = parser.parse_args()

    if args.all:
        device_types = DEVICE_TYPES
    elif args.device_type:
        device_types = [args.device_type]
    else:
        device_types = ["air-1"]   # sensible default

    client = SmartILabAPIClient(BASE_URL, API_KEY)

    # ── Startup banner ────────────────────────────────────────────────────────
    print()
    print(DLINE)
    print(_bold("  Smart i-Lab — Live Pipeline Monitor"))
    print(DLINE)
    print(f"  API endpoint   : {BASE_URL}")
    print(f"  DB path        : {_storage.DUCKDB_PATH}")
    print(f"  Device types   : {', '.join(device_types)}")
    print(f"  Lookback       : {args.lookback} h  (used if DB is empty)")
    print(f"  Poll interval  : {'continuous every ' + str(args.poll) + 's' if args.poll > 0 else 'one-shot'}")
    print(f"  Mode           : {'CHECK ONLY (read-only)' if args.check_only else 'INGEST (read+write)'}")
    print(DLINE)

    # ── API connectivity check ────────────────────────────────────────────────
    print(f"\n  Testing API connectivity …")
    try:
        import requests
        r = requests.get(BASE_URL + "/air-1", headers={"X-API-KEY": API_KEY}, timeout=10)
        r.raise_for_status()
        print(f"  {_green('✓ API reachable')}  (HTTP {r.status_code})")
    except Exception as exc:
        print(f"  {_red('✗ API unreachable:')} {exc}")
        print(f"  Continuing anyway — all device results will show UNREACHABLE.")

    # ── Graceful Ctrl+C ───────────────────────────────────────────────────────
    running = [True]

    def _stop(sig, frame):
        print(f"\n{_yellow('Stopping after current cycle …')}")
        running[0] = False

    signal.signal(signal.SIGINT, _stop)

    # ── Main loop ─────────────────────────────────────────────────────────────
    cycle = 0
    while running[0]:
        cycle += 1
        t_cycle_start = time.perf_counter()

        results = []
        for device_type in device_types:
            if not running[0]:
                break
            result = _run_device_cycle(
                device_type=device_type,
                client=client,
                cycle=cycle,
                lookback_hours=args.lookback,
                check_only=args.check_only,
                force_rebuild=args.force_rebuild,
            )
            results.append(result)

        cycle_elapsed = time.perf_counter() - t_cycle_start
        _print_summary(results, cycle, cycle_elapsed)

        if args.poll <= 0 or not running[0]:
            break

        next_at = datetime.now() + timedelta(seconds=args.poll)
        print(_dim(f"  Next cycle at {next_at.strftime('%H:%M:%S')} (Ctrl+C to stop) …\n"))

        # Sleep in 1-second increments so Ctrl+C is responsive
        for _ in range(args.poll):
            if not running[0]:
                break
            time.sleep(1)

    print(_bold("Monitor exited."))


if __name__ == "__main__":
    main()
