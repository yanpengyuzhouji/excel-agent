"""End-to-end smoke test for the Excel workbook service core (no HTTP).

Covers: upload -> review edit (revision bump) -> confirm (atomic DB) ->
read-only SELECT ok, INSERT/DROP rejected, aggregation ok.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SAMPLE = r"D:\git\DB-GPT-0.8.1\DB-GPT-0.8.1\docker\examples\excel\Walmart_Sales.csv"


def main() -> int:
    if not Path(SAMPLE).exists():
        print("SKIP: sample file not found:", SAMPLE)
        return 0

    test_root = Path(tempfile.mkdtemp(prefix="ews_smoke_"))
    import app.config as cfgmod

    cfgmod._config_cache = {
        "server": {
            "host": "127.0.0.1",
            "port": 8090,
            "storage_path": str(test_root),
            "preview_page_size": 100,
            "query_row_limit": 500,
        },
        "excel": {"llm": {}, "agent": {}},
    }
    from app.api.schemas import ConfirmRequest, DraftUpdateRequest
    from app.config import build_app_config
    from app.db import ensure_tables
    from app.service.service import WorkbookImportService

    cfg = build_app_config()
    svc = WorkbookImportService(config=cfg)
    svc.init_app()
    ensure_tables()

    content = Path(SAMPLE).read_bytes()
    imp = svc.create_import(Path(SAMPLE).name, content, "agent")
    assert imp.status == "ready", imp.status
    print("upload ->", imp.status, "rev", imp.revision)

    sid = imp.sheets[0]["sheet_id"]
    col = imp.sheets[0]["columns"][0]
    imp2 = svc.update_draft(
        imp.import_id,
        DraftUpdateRequest(
            revision=imp.revision,
            column_updates=[
                {
                    "sheet_id": sid,
                    "column_id": col["column_id"],
                    "physical_name": "store_id",
                }
            ],
        ),
    )
    assert imp2.revision == imp.revision + 1, "revision must bump"
    print("edit -> rev", imp2.revision)

    # cell-level edit: change the first data row's store_id to a numeric string
    imp3 = svc.update_draft(
        imp.import_id,
        DraftUpdateRequest(
            revision=imp2.revision,
            cell_updates=[
                {
                    "sheet_id": sid,
                    "column_id": col["column_id"],
                    "source_row": 2,  # first data row (row 1 is the header)
                    "value": "999",
                }
            ],
        ),
    )
    assert imp3.revision == imp2.revision + 1, "cell edit must bump revision"
    rows_data = svc.get_sheet_rows(imp.import_id, sid, 0, 5)
    first = rows_data["rows"][0]
    assert str(first["values"][0]) == "999", first["values"]
    assert col["column_id"] in first.get("edited_columns", []), first
    print("cell edit -> rev", imp3.revision, "value", first["values"][0])

    accepted = [w["code"] for w in imp3.validation.get("warnings", [])]
    resp = svc.confirm_import(imp.import_id, imp3.revision, accepted)
    assert resp.status == "active"
    assert Path(resp.database_path).exists()
    print("confirm ->", resp.status)

    q = svc.execute_readonly_sql(imp.workbook_id, "SELECT * FROM data LIMIT 5")
    assert q.row_count == 5 and "store_id" in q.columns, q.columns
    print("query ->", q.row_count, "rows")

    # the cell edit must be reflected in the materialized database
    q2 = svc.execute_readonly_sql(
        imp.workbook_id,
        "SELECT store_id FROM data ORDER BY _source_row LIMIT 1",
    )
    assert q2.rows[0][0] == 999, q2.rows[0]
    print("cell edit materialized ->", q2.rows[0][0])

    for bad in ("INSERT INTO data VALUES (1)", "DROP TABLE data"):
        try:
            svc.execute_readonly_sql(imp.workbook_id, bad)
            raise AssertionError(f"not rejected: {bad}")
        except ValueError:
            pass
    print("write rejection -> ok")

    agg = svc.execute_readonly_sql(
        imp.workbook_id,
        "SELECT store_id, COUNT(*) c, SUM(weekly_sales) s "
        "FROM data GROUP BY store_id LIMIT 3",
    )
    assert agg.row_count == 3
    print("aggregation -> ok")

    # --- guardrails: tiny thresholds on an independent service instance ---
    tiny = build_app_config()
    tiny.max_import_bytes = 128
    tiny.max_import_rows = 2
    tiny_svc = WorkbookImportService(config=tiny)
    tiny_svc.init_app()

    try:
        tiny_svc.create_import("big.csv", b"x" * 200, "agent")
        raise AssertionError("byte guardrail did not trigger")
    except ValueError as exc:
        assert "import limit" in str(exc), str(exc)
    print("guardrail bytes -> rejected")

    tiny_csv = Path(test_root) / "rows3.csv"
    tiny_csv.write_bytes("序号,物料编码,数量\n1,A,3\n2,B,7\n3,C,9\n".encode("utf-8"))
    try:
        tiny_svc.create_import("rows3.csv", tiny_csv.read_bytes(), "agent")
        raise AssertionError("row guardrail did not trigger")
    except ValueError as exc:
        assert "row limit" in str(exc), str(exc)
    print("guardrail rows -> rejected")

    shutil.rmtree(test_root, ignore_errors=True)
    print("SMOKE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
