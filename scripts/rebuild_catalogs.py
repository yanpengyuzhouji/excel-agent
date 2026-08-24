"""Rebuild owner-scoped metadata catalogs for existing active workbooks.

Usage:
    .venv-linux/bin/python scripts/rebuild_catalogs.py --owner-id default

The command reads only each active SQLite schema/manifest and writes the
metadata database. It never copies business rows into the catalog.
"""

from __future__ import annotations

import argparse

from app.config import build_app_config
from app.db import ensure_tables
from app.models.workbook import WorkbookDatasetDao
from app.service.service import WorkbookImportService


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-id", default="default")
    args = parser.parse_args()

    config = build_app_config()
    config.resolved_storage_path()
    ensure_tables()
    service = WorkbookImportService(config=config)
    datasets = WorkbookDatasetDao().list_by_owner(args.owner_id, limit=1000)
    rebuilt = 0
    failed = 0
    for dataset in datasets:
        if not dataset.active_import_id:
            continue
        try:
            service.refresh_workbook_catalog(dataset.workbook_id)
            rebuilt += 1
            print(f"rebuilt {dataset.workbook_id}")
        except Exception as exc:  # pragma: no cover - operational CLI path
            failed += 1
            print(f"failed {dataset.workbook_id}: {exc}")
    print(f"catalog rebuild complete: rebuilt={rebuilt} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
