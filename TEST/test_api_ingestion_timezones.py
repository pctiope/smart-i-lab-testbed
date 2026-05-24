import importlib
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ["DUCKDB_READ_ONLY"] = "1"

_api = importlib.import_module("api_ingestion")


class TestApiIngestionTimestamps(unittest.TestCase):
    def test_stored_local_timestamp_converts_to_utc(self):
        stored = datetime(2026, 5, 24, 8, 22, 50)

        actual = _api._stored_timestamp_to_utc(stored)

        self.assertEqual(
            actual,
            datetime(2026, 5, 24, 0, 22, 50, tzinfo=timezone.utc),
        )

    def test_aware_timestamp_converts_to_utc(self):
        stored = datetime(
            2026,
            5,
            24,
            8,
            22,
            50,
            tzinfo=timezone(timedelta(hours=8)),
        )

        actual = _api._stored_timestamp_to_utc(stored)

        self.assertEqual(
            actual,
            datetime(2026, 5, 24, 0, 22, 50, tzinfo=timezone.utc),
        )


if __name__ == "__main__":
    unittest.main()
