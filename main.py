"""
main.py — Smart i-Lab pipeline orchestrator
============================================

Runs the full Bronze-Silver-Gold pipeline:
  1. check_and_sync  — compare API vs bronze timestamps, reinit if stale
    2. run_pipeline    — ingest → bronze → silver → gold
  3. demo_queries    — example DataLoader usage

Usage
-----
    python main.py                          # sync + ingest + preprocess (all types)
    python main.py --check-only             # only print timestamp comparison
    python main.py --device-type air-1      # single device type
    python main.py --lookback 48            # bootstrap from 48 h ago
    python main.py --no-silver              # skip bronze→silver step
    python main.py --no-gold                # skip silver→gold step
"""

import argparse
from datetime import datetime, timedelta, timezone
from importlib import import_module as _im

# ── Pipeline modules ──────────────────────────────────────────────────────────
_api      = _im("api_ingestion")
_b2s      = _im("bronze2silver_preprocess")
_s2g      = _im("silver2gold_preprocess")
_storage  = _im("CSV Training Data Code")

SmartILabAPIClient    = _api.SmartILabAPIClient
check_needs_update    = _api.check_needs_update
ingest_and_rebuild_bronze = _api.ingest_and_rebuild_bronze

run_bronze_to_silver  = _b2s.run_bronze_to_silver
run_silver_to_gold    = _s2g.run_silver_to_gold

DEVICE_TYPES                  = _storage.DEVICE_TYPES
get_latest_stored_timestamp   = _storage.get_latest_stored_timestamp
_db                           = _storage._db
_q                            = _storage._q
_silver_table                 = _storage._silver_table
_gold_table                   = _storage._gold_table
_table_exists                 = _storage._table_exists

BASE_URL = _api.BASE_URL
API_KEY  = _api.API_KEY


# =============================================================================
# Step 1 — Timestamp check
# =============================================================================

def check_and_sync(device_types: list[str], client: SmartILabAPIClient) -> dict[str, bool]:
    """
    For each device type, compare the latest API reading timestamp with the
    latest bronze DuckDB timestamp.  Returns a dict {device_type: needs_update}.
    """
    print("\n" + "=" * 60)
    print("Step 1 — API vs Bronze timestamp check")
    print("=" * 60)

    results: dict[str, bool] = {}
    for device_type in device_types:
        needs_update, reason = check_needs_update(device_type, client)
        status = "STALE" if needs_update else "OK"
        print(f"  [{status:5s}]  {device_type:<18s}  {reason}")
        results[device_type] = needs_update
    return results


# =============================================================================
# Step 2 — Ingest API → Parquet + Bronze
# =============================================================================

def run_ingest(
    device_types:  list[str],
    client:        SmartILabAPIClient,
    stale_map:     dict[str, bool],
    lookback_hours: int,
    force_rebuild:  bool,
) -> dict[str, int]:
    """
    Fetch new data from the API for every stale device type and rebuild their
    bronze DuckDB tables from the Hive-partitioned Parquet store.
    Returns {device_type: parquet_rows_written}.
    """
    print("\n" + "=" * 60)
    print("Step 2 — API ingest → Parquet store → Bronze tables")
    print("=" * 60)

    results: dict[str, int] = {}
    for device_type in device_types:
        if not (stale_map.get(device_type, False) or force_rebuild):
            print(f"  [{device_type}] Up to date — skipping ingest")
            results[device_type] = 0
            continue

        rows, _ = ingest_and_rebuild_bronze(
            device_type=device_type,
            client=client,
            lookback_hours=lookback_hours,
        )
        results[device_type] = rows

    return results


# =============================================================================
# Step 3 — Bronze → Silver preprocessing
# =============================================================================

def run_preprocess(device_types: list[str], rebuild: bool = False):
    """Apply per-device preprocessing and push results to silver DuckDB tables."""
    print("\n" + "=" * 60)
    print("Step 3 — Bronze → Silver preprocessing")
    print("=" * 60)

    for device_type in device_types:
        run_bronze_to_silver(device_type, rebuild=rebuild)


def run_gold(device_types: list[str], rebuild: bool = False):
    """Apply per-device silver to gold placeholder processing and push gold tables."""
    print("\n" + "=" * 60)
    print("Step 4 — Silver → Gold preprocessing")
    print("=" * 60)

    for device_type in device_types:
        run_silver_to_gold(device_type, rebuild=rebuild)


# =============================================================================
# Demo DataLoader queries (example usage for ML training code)
# =============================================================================

def demo_training_queries(device_type: str = "air-1"):
    """Show example DataLoader query modes against the gold layer when available."""
    from dataloader import DataLoader  # import here to avoid circular at module level

    print("\n" + "=" * 60)
    print(f"Demo — DataLoader query examples for '{device_type}'")
    print("=" * 60)

    dl = DataLoader(device_type)
    layer = "gold" if _table_exists(_gold_table(device_type)) else "silver"
    print(f"\n{layer.title()} table columns: {dl.column_names(layer=layer)}")
    print(f"Total rows in {layer}: {dl.row_count(layer=layer):,}")

    # Query 1: latest 5 rows
    df1 = dl.load_latest_n(5, layer=layer)
    print(f"\n[latest_n=5] Shape: {df1.shape}")
    if not df1.empty:
        print(df1.to_string(max_rows=5, max_cols=8))

    # Query 2: last 1 hour
    since = datetime.now() - timedelta(hours=1)
    df2 = dl.load_since(since, layer=layer)
    print(f"\n[since={since.strftime('%H:%M')}] Shape: {df2.shape}")

    # Query 3: config-driven
    df3 = dl.load_training_config({
        "latest_n": 10,
        "columns":  ["timestamp"] + [f"temp_s{i}" for i in range(1, 4)],
        "layer": layer,
    })
    print(f"\n[config: latest_n=10, temp_s1-s3] Shape: {df3.shape}")
    if not df3.empty:
        print(df3.head(5).to_string())

    # Query 4: raw SQL
    ts_col = "timestamp"
    df4 = dl.load_sql(
        f"SELECT COUNT(*) AS row_count, MIN({ts_col}) AS earliest, MAX({ts_col}) AS latest FROM {{table}}",
        layer=layer,
    )
    print(f"\n[load_sql summary]")
    if not df4.empty:
        print(df4.to_string(index=False))


# =============================================================================
# Full pipeline
# =============================================================================

def run_full_pipeline(
    device_types:   list[str],
    lookback_hours: int  = 24,
    check_only:     bool = False,
    skip_silver:    bool = False,
    skip_gold:      bool = False,
    force_rebuild:  bool = False,
    run_demo:       bool = False,
):
    client = SmartILabAPIClient(BASE_URL, API_KEY)

    # Step 1 — timestamp check
    stale_map = check_and_sync(device_types, client)

    if check_only:
        return

    # Step 2 — ingest
    run_ingest(device_types, client, stale_map, lookback_hours, force_rebuild)

    # Step 3 — bronze → silver
    if not skip_silver:
        run_preprocess(device_types, rebuild=force_rebuild)

    if not skip_gold and not skip_silver:
        run_gold(device_types, rebuild=force_rebuild)

    # Optional demo
    if run_demo:
        for dt in device_types:
            if _table_exists(_gold_table(dt)) or _table_exists(_silver_table(dt)):
                demo_training_queries(dt)
                break


# =============================================================================
# Entry point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Smart i-Lab Bronze-Silver-Gold pipeline")
    parser.add_argument("--device-type", default=None, choices=DEVICE_TYPES,
                        help="Run for a single device type (default: all)")
    parser.add_argument("--lookback",     type=int, default=24,
                        help="Bootstrap lookback in hours if no data exists (default: 24)")
    parser.add_argument("--check-only",   action="store_true",
                        help="Only compare API vs DB timestamps, do not ingest")
    parser.add_argument("--no-silver",    action="store_true",
                        help="Skip the bronze → silver preprocessing step")
    parser.add_argument("--no-gold",      action="store_true",
                        help="Skip the silver → gold preprocessing step")
    parser.add_argument("--force-rebuild", action="store_true",
                        help="Rebuild bronze, silver, and gold tables even if up to date")
    parser.add_argument("--demo",         action="store_true",
                        help="Run DataLoader demo queries after the pipeline")
    args = parser.parse_args()

    device_types = [args.device_type] if args.device_type else DEVICE_TYPES

    print("=" * 60)
    print("Smart i-Lab BSG Pipeline")
    print(f"Device types : {device_types}")
    print(f"Lookback     : {args.lookback} h")
    print(f"Check only   : {args.check_only}")
    print(f"Skip silver  : {args.no_silver}")
    print(f"Skip gold    : {args.no_gold}")
    print(f"Force rebuild: {args.force_rebuild}")
    print("=" * 60)

    run_full_pipeline(
        device_types=device_types,
        lookback_hours=args.lookback,
        check_only=args.check_only,
        skip_silver=args.no_silver,
        skip_gold=args.no_gold,
        force_rebuild=args.force_rebuild,
        run_demo=args.demo,
    )

    print("\nDone.")


if __name__ == "__main__":
    main()
