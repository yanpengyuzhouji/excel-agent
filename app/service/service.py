"""Workbook import service, ported from
DB-GPT packages/dbgpt-serve/src/dbgpt_serve/workbook/service/service.py.

Changes vs. the original:
- No SystemApp / BaseService: plain class wired via FastAPI app.state.
- No Result wrapper: returns Pydantic models / dicts; endpoints map errors via
  app.core.http_error.
- Workbooks are owner-scoped by the authenticated API principal.
- _register_datasource deleted (was DB-GPT-specific datasource integration);
  datasource_id is always None, datasource_name keeps the workbook_xxx prefix.
- execute_readonly_sql uses .as_posix() so the sqlite file URI works on Windows
  (Path.resolve() emits backslashes which break mode=ro URI parsing).
- Added list_imports() for the frontend "My Excel workbooks" panel.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..api.schemas import (
    ConfirmResponse,
    DraftUpdateRequest,
    SQLQueryResponse,
    WorkbookImportResponse,
)
from ..core import ResourceBusyError, RevisionConflictError
from ..models.workbook import (
    WorkbookDatasetDao,
    WorkbookDatasetEntity,
    WorkbookImportDao,
    WorkbookImportEntity,
)
from ..models.conversation import (
    WorkbookConversationEntity,
    WorkbookMessageEntity,
)
from .materializer import build_database as materialize_database
from .parser import dump_draft, load_draft, parse_workbook

logger = logging.getLogger(__name__)


def _quote_identifier(value: str) -> str:
    if not value or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return f'"{value}"'


def _summary_sheets(draft: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = []
    for sheet in draft.get("sheets", []):
        result.append(
            {k: v for k, v in sheet.items() if k not in {"rows", "merge_lineage"}}
        )
    return result


class WorkbookImportService:
    """Upload -> parse -> review draft -> confirm -> read-only SQL -> reports."""

    def __init__(
        self,
        config: Any = None,
        dataset_dao: Optional[WorkbookDatasetDao] = None,
        import_dao: Optional[WorkbookImportDao] = None,
    ):
        self._config = config
        self._dataset_dao = dataset_dao or WorkbookDatasetDao()
        self._import_dao = import_dao or WorkbookImportDao()
        self._import_slots = threading.BoundedSemaphore(
            int(getattr(config, "max_concurrent_imports", 2))
        )
        self._sql_slots = threading.BoundedSemaphore(
            int(getattr(config, "max_concurrent_sql", 4))
        )
        self._agent_slots = threading.BoundedSemaphore(
            int(getattr(config, "max_concurrent_agent_runs", 2))
        )

    @property
    def config(self) -> Any:
        return self._config

    def init_app(self) -> None:
        Path(self._config.resolved_storage_path()).mkdir(parents=True, exist_ok=True)

    def try_acquire_agent_run(self) -> bool:
        return self._agent_slots.acquire(blocking=False)

    def release_agent_run(self) -> None:
        self._agent_slots.release()

    def _parser_limits(self) -> Dict[str, Any]:
        return {
            "max_rows": getattr(self._config, "max_import_rows", 100_000),
            "max_columns": getattr(self._config, "max_import_columns", 512),
            "max_cells": getattr(self._config, "max_import_cells", 2_000_000),
            "max_cell_chars": getattr(self._config, "max_cell_chars", 32_767),
            "max_total_text_chars": getattr(
                self._config, "max_total_text_chars", 64 * 1024 * 1024
            ),
            "max_uncompressed_bytes": getattr(
                self._config, "max_uncompressed_bytes", 256 * 1024 * 1024
            ),
            "max_zip_ratio": getattr(self._config, "max_zip_ratio", 200.0),
            "max_zip_entries": getattr(self._config, "max_zip_entries", 10_000),
            "max_merged_cells": getattr(
                self._config, "max_merged_cells", 200_000
            ),
            "max_xml_entry_bytes": getattr(
                self._config, "max_xml_entry_bytes", 64 * 1024 * 1024
            ),
        }

    def _resolve_version_artifact_path(
        self, workbook_id: str, version: int, stored_path: Optional[str]
    ) -> Optional[Path]:
        """Resolve an artifact path saved by this service on another OS.

        The metadata database can outlive the process that created it.  For
        example, a Windows run stores ``D:\\git\\...\\database.sqlite``;
        when the same workspace is opened from WSL, that literal path does
        not exist even though ``data/workbook_imports/<id>/v<version>`` does.
        Keep the persisted value untouched for cross-OS portability and map
        it to the current configured storage root only when the original path
        is unavailable.
        """
        if not stored_path:
            return None
        direct = Path(stored_path)
        if direct.exists():
            return direct
        artifact_name = re.split(r"[\\\\/]", str(stored_path))[-1]
        if not artifact_name:
            return direct
        candidate = (
            Path(self._config.resolved_storage_path())
            / workbook_id
            / f"v{int(version)}"
            / artifact_name
        )
        if candidate.exists():
            logger.debug(
                "Resolving stale persisted artifact path for workbook %s: %s -> %s",
                workbook_id,
                stored_path,
                candidate,
            )
            return candidate
        return direct

    # ------------------------------------------------------------------ reads

    def _load_entity(
        self, import_id: str, owner_id: Optional[str] = None
    ) -> WorkbookImportEntity:
        entity = self._import_dao.get_by_import_id(import_id)
        if not entity:
            raise ValueError(f"Workbook import not found: {import_id}")
        if owner_id is not None and not self._dataset_dao.get_by_workbook_id(
            entity.workbook_id, owner_id=owner_id
        ):
            raise ValueError(f"Workbook import not found: {import_id}")
        return entity

    def _dataset_for(
        self, workbook_id: str, owner_id: Optional[str] = None
    ) -> WorkbookDatasetEntity:
        dataset = self._dataset_dao.get_by_workbook_id(workbook_id, owner_id=owner_id)
        if not dataset:
            raise ValueError(f"Workbook not found: {workbook_id}")
        return dataset

    def assert_workbook_owner(self, workbook_id: str, owner_id: str) -> None:
        self._dataset_for(workbook_id, owner_id=owner_id)

    def assert_import_owner(self, import_id: str, owner_id: str) -> None:
        self._load_entity(import_id, owner_id=owner_id)

    def get_import(self, import_id: str) -> WorkbookImportResponse:
        entity = self._load_entity(import_id)
        dataset = self._dataset_dao.get_by_workbook_id(entity.workbook_id)
        sheets: List[Dict[str, Any]] = []
        draft_path = self._resolve_version_artifact_path(
            entity.workbook_id, entity.version, entity.draft_path
        )
        if draft_path and draft_path.exists():
            sheets = _summary_sheets(load_draft(str(draft_path)))
        validation = json.loads(entity.validation_json or "{}")
        return WorkbookImportResponse(
            import_id=entity.import_id,
            workbook_id=entity.workbook_id,
            source_channel=entity.source_channel,
            file_name=entity.file_name,
            version=entity.version,
            status=entity.status,
            revision=entity.revision,
            datasource_name=dataset.datasource_name if dataset else None,
            active_version=dataset.active_version if dataset else None,
            sheets=sheets,
            validation=validation,
            error_message=entity.error_message,
        )

    def list_imports(
        self,
        status: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
        owner_id: Optional[str] = None,
    ) -> List[WorkbookImportResponse]:
        entities = self._import_dao.list_imports(
            status=status, limit=limit, offset=offset, owner_id=owner_id
        )
        return [self.get_import(entity.import_id) for entity in entities]

    def get_sheet_rows(
        self,
        import_id: str,
        sheet_id: str,
        offset: int = 0,
        limit: int = 100,
    ) -> Dict[str, Any]:
        entity = self._load_entity(import_id)
        draft_path = self._resolve_version_artifact_path(
            entity.workbook_id, entity.version, entity.draft_path
        )
        if not draft_path or not draft_path.exists():
            raise ValueError("Workbook draft file does not exist")
        draft = load_draft(str(draft_path))
        sheet = next(
            (item for item in draft["sheets"] if item["sheet_id"] == sheet_id), None
        )
        if not sheet:
            raise ValueError(f"Sheet not found: {sheet_id}")
        rows = sheet.get("rows", [])
        return {
            "sheet_id": sheet_id,
            "columns": sheet.get("columns", []),
            "offset": offset,
            "limit": limit,
            "total": len(rows),
            "rows": rows[offset : offset + limit],
        }

    # ------------------------------------------------------------------ write

    def create_import(
        self,
        file_name: str,
        content: bytes,
        source_channel: str,
        workbook_id: Optional[str] = None,
        owner_id: str = "default",
    ) -> WorkbookImportResponse:
        return self._create_import(
            file_name=file_name,
            source_channel=source_channel,
            workbook_id=workbook_id,
            owner_id=owner_id,
            file_size=len(content),
            source_hash=hashlib.sha256(content).hexdigest(),
            write_source=lambda path: path.write_bytes(content),
        )

    def create_import_from_path(
        self,
        file_name: str,
        staged_path: str,
        source_channel: str,
        workbook_id: Optional[str] = None,
        owner_id: str = "default",
        file_size: Optional[int] = None,
        source_hash: Optional[str] = None,
    ) -> WorkbookImportResponse:
        """Import an already bounded upload without loading it into memory."""
        source = Path(staged_path)
        actual_size = source.stat().st_size
        if file_size is not None and actual_size != file_size:
            raise ValueError("Staged upload size changed before import")
        if source_hash is None:
            digest = hashlib.sha256()
            with source.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            source_hash = digest.hexdigest()

        def copy_source(destination: Path) -> None:
            with source.open("rb") as input_stream, destination.open("xb") as output:
                shutil.copyfileobj(input_stream, output, length=1024 * 1024)

        return self._create_import(
            file_name=file_name,
            source_channel=source_channel,
            workbook_id=workbook_id,
            owner_id=owner_id,
            file_size=actual_size,
            source_hash=source_hash,
            write_source=copy_source,
        )

    def _create_import(
        self,
        *,
        file_name: str,
        source_channel: str,
        workbook_id: Optional[str],
        owner_id: str,
        file_size: int,
        source_hash: str,
        write_source: Callable[[Path], object],
    ) -> WorkbookImportResponse:
        if source_channel not in {"chat_excel", "agent"}:
            raise ValueError("source_channel must be chat_excel or agent")
        if file_size <= 0:
            raise ValueError("Uploaded file is empty")
        suffix = Path(file_name).suffix.lower()
        if suffix not in {".xlsx", ".xlsm", ".csv"}:
            raise ValueError("Only .xlsx, .xlsm and .csv files are supported")
        max_bytes = getattr(self._config, "max_import_bytes", 200 * 1024 * 1024)
        if file_size > max_bytes:
            limit_mb = max_bytes / (1024 * 1024)
            raise ValueError(
                f"Workbook {file_name!r} is {file_size / (1024 * 1024):.1f} MB, "
                f"over the {limit_mb:.0f} MB import limit "
                f"(config: server.max_import_bytes)"
            )

        if not self._import_slots.acquire(blocking=False):
            raise ResourceBusyError("Import concurrency limit reached; retry later")
        try:
            return self._create_import_locked(
                file_name=file_name,
                suffix=suffix,
                source_channel=source_channel,
                workbook_id=workbook_id,
                owner_id=owner_id,
                source_hash=source_hash,
                write_source=write_source,
            )
        finally:
            self._import_slots.release()

    def _create_import_locked(
        self, *, file_name: str, suffix: str, source_channel: str,
        workbook_id: Optional[str], owner_id: str, source_hash: str,
        write_source: Callable[[Path], object],
    ) -> WorkbookImportResponse:
        dataset: Optional[WorkbookDatasetEntity] = None
        if workbook_id:
            dataset = self._dataset_dao.get_by_workbook_id(
                workbook_id, owner_id=owner_id
            )
            if not dataset:
                raise ValueError(f"Workbook not found: {workbook_id}")
        else:
            workbook_id = uuid.uuid4().hex
            datasource_name = f"workbook_{workbook_id[:16]}"
            dataset = self._dataset_dao.create_entity(
                {
                    "workbook_id": workbook_id,
                    "owner_id": owner_id,
                    "display_name": Path(file_name).stem,
                    "datasource_name": datasource_name,
                }
            )

        version = self._dataset_dao.allocate_next_version(workbook_id)
        import_id = uuid.uuid4().hex
        version_dir = (
            Path(self._config.resolved_storage_path())
            / workbook_id
            / f"v{version}"
        )
        version_dir.mkdir(parents=True, exist_ok=True)
        safe_source_name = f"source{suffix}"
        source_path = version_dir / safe_source_name
        draft_path = version_dir / "draft.json"
        write_source(source_path)
        self._import_dao.create_entity(
            {
                "import_id": import_id,
                "workbook_id": workbook_id,
                "version": version,
                "source_channel": source_channel,
                "file_name": file_name,
                "source_hash": source_hash,
                "source_path": str(source_path),
                "draft_path": str(draft_path),
                "status": "parsing",
                "revision": 1,
            }
        )
        try:
            draft = parse_workbook(
                str(source_path),
                file_name=file_name,
                **self._parser_limits(),
            )
            draft.update(
                {
                    "import_id": import_id,
                    "workbook_id": workbook_id,
                    "version": version,
                    "source_hash": source_hash,
                }
            )
            dump_draft(draft, str(draft_path))
            validation = self._validate_draft(draft)
            status = "ready" if not validation["errors"] else "draft"
            self._import_dao.update_entity(
                import_id,
                {
                    "status": status,
                    "summary_json": json.dumps(
                        {"sheets": _summary_sheets(draft)}, ensure_ascii=False
                    ),
                    "validation_json": json.dumps(validation, ensure_ascii=False),
                },
            )
        except Exception as exc:
            self._import_dao.update_entity(
                import_id, {"status": "failed", "error_message": str(exc)}
            )
            raise
        return self.get_import(import_id)

    def update_draft(
        self, import_id: str, request: DraftUpdateRequest
    ) -> WorkbookImportResponse:
        entity = self._load_entity(import_id)
        if entity.status == "updating":
            raise RevisionConflictError("Draft is currently being updated")
        if entity.status in {"committing", "active", "superseded"}:
            raise ValueError(f"Import in status {entity.status} cannot be edited")
        if entity.revision != request.revision:
            raise RevisionConflictError(
                "Draft revision changed: "
                f"expected {request.revision}, current {entity.revision}"
            )
        previous_status = entity.status
        if not self._import_dao.claim_draft_revision(
            import_id, request.revision, previous_status
        ):
            current = self._load_entity(import_id)
            if current.revision != request.revision:
                raise RevisionConflictError(
                    "Draft revision changed: "
                    f"expected {request.revision}, current {current.revision}"
                )
            raise RevisionConflictError(
                f"Draft revision {request.revision} is already being updated"
            )

        draft_path = Path(entity.draft_path)
        pending_path = draft_path.with_name(
            f".{draft_path.name}.{uuid.uuid4().hex}.pending"
        )
        backup_path = draft_path.with_name(
            f".{draft_path.name}.{uuid.uuid4().hex}.backup"
        )
        draft_swapped = False
        try:
            draft = self._apply_draft_request(entity, request)
            validation = self._validate_draft(draft)
            dump_draft(draft, str(pending_path))
            os.replace(draft_path, backup_path)
            os.replace(pending_path, draft_path)
            draft_swapped = True
            if not self._import_dao.complete_draft_revision(
                import_id,
                request.revision,
                {
                    "status": "ready" if not validation["errors"] else "draft",
                    "summary_json": json.dumps(
                        {"sheets": _summary_sheets(draft)}, ensure_ascii=False
                    ),
                    "validation_json": json.dumps(validation, ensure_ascii=False),
                },
            ):
                raise RevisionConflictError(
                    f"Draft revision {request.revision} changed during update"
                )
            backup_path.unlink(missing_ok=True)
            return self.get_import(import_id)
        except Exception:
            if draft_swapped and backup_path.exists():
                os.replace(backup_path, draft_path)
            self._import_dao.release_draft_revision(
                import_id, request.revision, previous_status
            )
            raise
        finally:
            pending_path.unlink(missing_ok=True)
            backup_path.unlink(missing_ok=True)

    def _apply_draft_request(
        self, entity: WorkbookImportEntity, request: DraftUpdateRequest
    ) -> Dict[str, Any]:
        draft = load_draft(entity.draft_path)
        # Structural row changes invalidate every derived field. Re-read the
        # immutable source once, then replace only the affected sheets before
        # applying column/cell edits from this request.
        structural_overrides: Dict[str, Dict[str, int]] = {}
        current_sheets = {item["sheet_id"]: item for item in draft["sheets"]}
        for update in request.sheet_updates:
            sheet = current_sheets.get(update.get("sheet_id"))
            if not sheet:
                raise ValueError(f"Unknown sheet: {update.get('sheet_id')}")
            header_row = int(update.get("header_row", sheet.get("header_row") or 0))
            data_start_row = int(
                update.get("data_start_row", sheet.get("data_start_row") or 0)
            )
            if (
                header_row != int(sheet.get("header_row") or 0)
                or data_start_row != int(sheet.get("data_start_row") or 0)
            ):
                structural_overrides[sheet["sheet_id"]] = {
                    "header_row": header_row,
                    "data_start_row": data_start_row,
                }

        if structural_overrides:
            reparsed = parse_workbook(
                entity.source_path,
                file_name=entity.file_name,
                **self._parser_limits(),
                sheet_overrides=structural_overrides,
            )
            reparsed_by_id = {item["sheet_id"]: item for item in reparsed["sheets"]}
            refreshed_names = set()
            for index, old_sheet in enumerate(draft["sheets"]):
                if old_sheet["sheet_id"] not in structural_overrides:
                    continue
                new_sheet = reparsed_by_id[old_sheet["sheet_id"]]
                new_sheet["enabled"] = old_sheet.get("enabled", True)
                if old_sheet.get("table_name"):
                    new_sheet["table_name"] = old_sheet["table_name"]
                draft["sheets"][index] = new_sheet
                refreshed_names.add(new_sheet["name"])
            draft["metadata"] = [
                item for item in draft.get("metadata", [])
                if item.get("source_sheet") not in refreshed_names
            ] + [
                item for item in reparsed.get("metadata", [])
                if item.get("source_sheet") in refreshed_names
            ]

        sheets = {item["sheet_id"]: item for item in draft["sheets"]}
        for update in request.sheet_updates:
            sheet = sheets.get(update.get("sheet_id"))
            if not sheet:
                raise ValueError(f"Unknown sheet: {update.get('sheet_id')}")
            for key in (
                "enabled",
                "kind",
                "table_name",
                "header_row",
                "data_start_row",
            ):
                if key in update:
                    sheet[key] = update[key]
        for update in request.column_updates:
            sheet = sheets.get(update.get("sheet_id"))
            if not sheet:
                raise ValueError(f"Unknown sheet: {update.get('sheet_id')}")
            column = next(
                (
                    item
                    for item in sheet.get("columns", [])
                    if item["column_id"] == update.get("column_id")
                ),
                None,
            )
            if not column:
                raise ValueError(f"Unknown column: {update.get('column_id')}")
            for key in (
                "enabled",
                "display_name",
                "physical_name",
                "sql_type",
                "nullable",
            ):
                if key in update:
                    column[key] = update[key]
        for update in request.cell_updates:
            sheet = sheets.get(update.get("sheet_id"))
            if not sheet or sheet.get("kind") != "data":
                raise ValueError(f"Unknown data sheet: {update.get('sheet_id')}")
            source_row = int(update["source_row"])
            column_id = update["column_id"]
            column_index = next(
                (
                    index
                    for index, item in enumerate(sheet["columns"])
                    if item["column_id"] == column_id
                ),
                None,
            )
            if column_index is None:
                raise ValueError(f"Unknown column: {column_id}")
            row = next(
                (item for item in sheet["rows"] if item["source_row"] == source_row),
                None,
            )
            if not row:
                raise ValueError(f"Unknown source row: {source_row}")
            row["values"][column_index] = update.get("value")
            row.setdefault("edited_columns", [])
            if column_id not in row["edited_columns"]:
                row["edited_columns"].append(column_id)
        return draft

    def validate_import(self, import_id: str) -> WorkbookImportResponse:
        entity = self._load_entity(import_id)
        if entity.status == "updating":
            raise RevisionConflictError("Draft is currently being updated")
        draft = load_draft(entity.draft_path)
        validation = self._validate_draft(draft)
        self._import_dao.update_entity(
            import_id,
            {
                "status": "ready" if not validation["errors"] else "draft",
                "validation_json": json.dumps(validation, ensure_ascii=False),
            },
        )
        return self.get_import(import_id)

    @staticmethod
    def _validate_draft(draft: Dict[str, Any]) -> Dict[str, Any]:
        errors: List[Dict[str, Any]] = []
        warnings: List[Dict[str, Any]] = []
        data_sheets = [
            sheet
            for sheet in draft.get("sheets", [])
            if sheet.get("enabled") and sheet.get("kind") == "data"
        ]
        if not data_sheets:
            errors.append(
                {
                    "code": "NO_DATA_SHEET",
                    "level": "error",
                    "message": "至少需要一个启用的数据表",
                }
            )
        table_names: set[str] = set()
        for sheet in data_sheets:
            sheet_id = sheet["sheet_id"]
            table_name = sheet.get("table_name", "")
            try:
                _quote_identifier(table_name)
            except ValueError:
                errors.append(
                    {
                        "code": "INVALID_TABLE_NAME",
                        "level": "error",
                        "message": f"表名非法: {table_name}",
                        "sheet_id": sheet_id,
                    }
                )
            if table_name in table_names:
                errors.append(
                    {
                        "code": "DUPLICATE_TABLE_NAME",
                        "level": "error",
                        "message": f"表名重复: {table_name}",
                        "sheet_id": sheet_id,
                    }
                )
            table_names.add(table_name)
            names: set[str] = set()
            for column in [
                item for item in sheet.get("columns", []) if item.get("enabled")
            ]:
                name = column.get("physical_name", "")
                try:
                    _quote_identifier(name)
                except ValueError:
                    errors.append(
                        {
                            "code": "INVALID_COLUMN_NAME",
                            "level": "error",
                            "message": f"列名非法: {name}",
                            "sheet_id": sheet_id,
                            "column": name,
                        }
                    )
                if name in names:
                    errors.append(
                        {
                            "code": "DUPLICATE_COLUMN_NAME",
                            "level": "error",
                            "message": f"列名重复: {name}",
                            "sheet_id": sheet_id,
                            "column": name,
                        }
                    )
                names.add(name)
                if column.get("empty_header"):
                    warnings.append(
                        {
                            "code": "EMPTY_HEADER",
                            "level": "warning",
                            "message": (
                                f"列 {column.get('source_column')} 没有表头，"
                                f"已建议为 {column.get('display_name')}"
                            ),
                            "sheet_id": sheet_id,
                            "column": name,
                        }
                    )
                if column.get("mixed_numeric"):
                    warnings.append(
                        {
                            "code": "MIXED_NUMERIC_PLACEHOLDER",
                            "level": "warning",
                            "message": (
                                f"{column.get('display_name')} 混合了数值和占位符，"
                                "将同时存储数值列与原始列"
                            ),
                            "sheet_id": sheet_id,
                            "column": name,
                            "count": column.get("placeholder_count", 0),
                        }
                    )
            if sheet.get("hidden_rows"):
                warnings.append(
                    {
                        "code": "HIDDEN_ROWS",
                        "level": "warning",
                        "message": "包含隐藏源行",
                        "sheet_id": sheet_id,
                        "count": len(sheet["hidden_rows"]),
                    }
                )
            if sheet.get("merged_ranges"):
                warnings.append(
                    {
                        "code": "MERGED_CELLS",
                        "level": "warning",
                        "message": "合并单元格的值会向下填充，并保留来源追溯",
                        "sheet_id": sheet_id,
                        "count": len(sheet["merged_ranges"]),
                    }
                )
            if sheet.get("formulas"):
                warnings.append(
                    {
                        "code": "FORMULAS",
                        "level": "warning",
                        "message": "导入公式的缓存值，并保留公式本身",
                        "sheet_id": sheet_id,
                        "count": len(sheet["formulas"]),
                    }
                )
        return {
            "errors": errors,
            "warnings": warnings,
            "error_count": len(errors),
            "warning_count": len(warnings),
        }

    def confirm_import(
        self,
        import_id: str,
        revision: int,
        accepted_warning_codes: List[str],
    ) -> ConfirmResponse:
        entity = self._load_entity(import_id)
        if entity.revision != revision:
            raise RevisionConflictError(
                "Draft revision changed: "
                f"expected {revision}, current {entity.revision}"
            )
        if entity.status not in {"draft", "ready"}:
            raise ValueError(f"Import in status {entity.status} cannot be committed")
        draft = load_draft(entity.draft_path)
        validation = self._validate_draft(draft)
        if validation["errors"]:
            raise ValueError("Validation errors must be fixed before commit")
        required_codes = {item["code"] for item in validation["warnings"]}
        missing = required_codes - set(accepted_warning_codes)
        if missing:
            raise ValueError(
                f"Warnings require acknowledgement: {', '.join(sorted(missing))}"
            )
        dataset = self._dataset_dao.get_by_workbook_id(entity.workbook_id)
        if not dataset:
            raise ValueError("Logical workbook was deleted")
        version_dir = Path(entity.draft_path).parent
        tmp_path = version_dir / "database.tmp.sqlite"
        final_path = version_dir / "database.sqlite"
        if tmp_path.exists():
            tmp_path.unlink()
        self._import_dao.update_entity(import_id, {"status": "committing"})
        try:
            self._build_database(draft, entity, str(tmp_path))
            os.replace(tmp_path, final_path)
            self._import_dao.mark_other_versions_superseded(
                entity.workbook_id, entity.import_id
            )
            self._import_dao.update_entity(
                import_id,
                {
                    "status": "active",
                    "database_path": str(final_path),
                    "accepted_warnings_json": json.dumps(
                        sorted(required_codes), ensure_ascii=False
                    ),
                },
            )
            self._dataset_dao.update_entity(
                entity.workbook_id,
                {
                    "active_version": entity.version,
                    "active_import_id": entity.import_id,
                },
            )
        except Exception as exc:
            if tmp_path.exists():
                tmp_path.unlink()
            self._import_dao.update_entity(
                import_id, {"status": "failed", "error_message": str(exc)}
            )
            raise
        return ConfirmResponse(
            import_id=import_id,
            workbook_id=entity.workbook_id,
            version=entity.version,
            status="active",
            datasource_id=None,
            datasource_name=dataset.datasource_name,
            database_path=str(final_path),
        )

    @staticmethod
    def _build_database(
        draft: Dict[str, Any], entity: WorkbookImportEntity, database_path: str
    ) -> None:
        materialize_database(
            draft,
            database_path,
            import_id=entity.import_id,
            workbook_id=entity.workbook_id,
            version=entity.version,
            file_name=entity.file_name,
            source_hash=entity.source_hash,
        )

    def activate_version(self, workbook_id: str, version: int) -> ConfirmResponse:
        dataset = self._dataset_dao.get_by_workbook_id(workbook_id)
        if not dataset:
            raise ValueError(f"Workbook not found: {workbook_id}")
        with self._import_dao.session() as session:
            entity = (
                session.query(WorkbookImportEntity)
                .filter(
                    WorkbookImportEntity.workbook_id == workbook_id,
                    WorkbookImportEntity.version == version,
                    WorkbookImportEntity.database_path.isnot(None),
                )
                .first()
            )
            if entity:
                session.expunge(entity)
        database_path = (
            self._resolve_version_artifact_path(
                workbook_id,
                entity.version,
                entity.database_path,
            )
            if entity
            else None
        )
        if not entity or not database_path or not database_path.exists():
            raise ValueError(f"Committed workbook version not found: {version}")
        self._import_dao.mark_other_versions_superseded(workbook_id, entity.import_id)
        self._import_dao.update_entity(entity.import_id, {"status": "active"})
        self._dataset_dao.update_entity(
            workbook_id,
            {"active_version": version, "active_import_id": entity.import_id},
        )
        return ConfirmResponse(
            import_id=entity.import_id,
            workbook_id=workbook_id,
            version=version,
            status="active",
            datasource_id=None,
            datasource_name=dataset.datasource_name,
            database_path=str(database_path),
        )

    def delete_workbook(self, workbook_id: str) -> None:
        """Delete a workbook: metadata rows + all on-disk artifacts.

        Removes the dataset row, every import row (all versions), and the
        workbook directory on disk (sources, drafts, databases, reports).
        Raises ValueError if the workbook does not exist.
        """
        dataset = self._dataset_dao.get_by_workbook_id(workbook_id)
        if not dataset:
            raise ValueError(f"Workbook not found: {workbook_id}")

        storage_base = Path(self._config.resolved_storage_path()).resolve()
        storage_root = (storage_base / workbook_id).resolve()
        if storage_root.parent != storage_base:
            raise ValueError("Unsafe workbook storage path")

        # Keep relational cleanup atomic. Messages must be removed before their
        # conversation rows because legacy installations did not have FK
        # cascades enabled.
        with self._import_dao.session() as session:
            conversation_ids = session.query(
                WorkbookConversationEntity.conv_id
            ).filter(WorkbookConversationEntity.workbook_id == workbook_id)
            session.query(WorkbookMessageEntity).filter(
                WorkbookMessageEntity.conv_id.in_(conversation_ids)
            ).delete(synchronize_session=False)
            session.query(WorkbookConversationEntity).filter(
                WorkbookConversationEntity.workbook_id == workbook_id
            ).delete(synchronize_session=False)
            session.query(WorkbookImportEntity).filter(
                WorkbookImportEntity.workbook_id == workbook_id
            ).delete(synchronize_session=False)
            session.query(WorkbookDatasetEntity).filter(
                WorkbookDatasetEntity.workbook_id == workbook_id
            ).delete(synchronize_session=False)
        # Remove the workbook directory on disk (v*/ source/draft/database/reports).
        if storage_root.exists():
            try:
                import shutil

                shutil.rmtree(storage_root)
            except OSError:
                logger.warning("Failed to remove workbook dir %s", storage_root)
        logger.info("Deleted workbook %s", workbook_id)

    def active_database_path(self, workbook_id: str) -> str:
        dataset = self._dataset_dao.get_by_workbook_id(workbook_id)
        if not dataset or not dataset.active_import_id:
            raise ValueError("Workbook has no active database")
        entity = self._load_entity(dataset.active_import_id)
        database_path = self._resolve_version_artifact_path(
            workbook_id, entity.version, entity.database_path
        )
        if not database_path or not database_path.exists():
            raise ValueError("Active workbook database file does not exist")
        return str(database_path)

    def execute_readonly_sql(
        self,
        workbook_id: str,
        sql: str,
        max_rows: int = 500,
    ) -> SQLQueryResponse:
        if not self._sql_slots.acquire(blocking=False):
            raise ResourceBusyError("SQL concurrency limit reached; retry later")
        try:
            dataset = self._dataset_dao.get_by_workbook_id(workbook_id)
            if not dataset or not dataset.active_import_id:
                raise ValueError("Workbook has no active database")
            entity = self._load_entity(dataset.active_import_id)
            database_path = self._resolve_version_artifact_path(
                workbook_id, getattr(entity, "version", 1), entity.database_path
            )
            if not database_path or not database_path.exists():
                raise ValueError("Active workbook database file does not exist")
            self._validate_readonly_sql(sql)
            configured_rows = int(getattr(self._config, "query_row_limit", 500))
            max_rows = min(max(int(max_rows), 1), configured_rows)
            timeout_ms = max(1, int(getattr(self._config, "sql_timeout_ms", 5_000)))
            max_vm_steps = max(1, int(getattr(self._config, "sql_vm_steps", 2_000_000)))
            progress_granularity = min(1_000, max_vm_steps)
            started = time.perf_counter()
            uri = f"file:{database_path.resolve().as_posix()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=1.0)
            interrupted_by = None
            vm_steps = 0
            try:
                forbidden_actions = {
                    sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE,
                    sqlite3.SQLITE_DELETE, sqlite3.SQLITE_CREATE_TABLE,
                    sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE,
                    sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH,
                    sqlite3.SQLITE_PRAGMA,
                }

                def authorizer(action, _arg1, _arg2, _db, _trigger):
                    return sqlite3.SQLITE_DENY if action in forbidden_actions else sqlite3.SQLITE_OK

                def progress_handler():
                    nonlocal interrupted_by, vm_steps
                    vm_steps += progress_granularity
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    if vm_steps >= max_vm_steps:
                        interrupted_by = f"VM step limit ({max_vm_steps})"
                        return 1
                    if elapsed_ms >= timeout_ms:
                        interrupted_by = f"timeout ({timeout_ms} ms)"
                        return 1
                    return 0

                connection.set_authorizer(authorizer)
                connection.set_progress_handler(
                    progress_handler, progress_granularity
                )
                try:
                    cursor = connection.execute(sql.strip().rstrip(";"))
                    columns = [item[0] for item in cursor.description or []]
                    rows = cursor.fetchmany(max_rows + 1)
                except sqlite3.OperationalError as exc:
                    if interrupted_by and "interrupt" in str(exc).lower():
                        raise ValueError(f"SQL query exceeded {interrupted_by}") from exc
                    raise
                truncated = len(rows) > max_rows
                rows = rows[:max_rows]
            finally:
                connection.set_progress_handler(None, 0)
                connection.close()
        finally:
            self._sql_slots.release()
        elapsed = (time.perf_counter() - started) * 1000
        return SQLQueryResponse(
            sql=sql.strip(),
            columns=columns,
            rows=[list(row) for row in rows],
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=round(elapsed, 3),
        )

    @staticmethod
    def _validate_readonly_sql(sql: str) -> None:
        value = re.sub(r"/\*.*?\*/|--[^\n]*", " ", sql, flags=re.S).strip()
        if not value:
            raise ValueError("SQL is empty")
        if ";" in value.rstrip(";"):
            raise ValueError("Only one SQL statement is allowed")
        try:
            import sqlglot
            from sqlglot import expressions as exp

            statements = sqlglot.parse(value, read="sqlite")
            if len(statements) != 1:
                raise ValueError("Only one SQL statement is allowed")
            tree = statements[0]
            forbidden = (
                exp.Insert,
                exp.Update,
                exp.Delete,
                exp.Create,
                exp.Drop,
                exp.Alter,
                exp.Command,
            )
            if isinstance(tree, forbidden) or any(
                tree.find(kind) for kind in forbidden
            ):
                raise ValueError("Only read-only SELECT statements are allowed")
            if not tree.find(exp.Select):
                raise ValueError("Only SELECT statements are allowed")
        except ImportError:
            if not re.match(r"(?is)^(select|with)\b", value):
                raise ValueError("Only SELECT statements are allowed")
            if re.search(
                r"(?is)\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum)\b",
                value,
            ):
                raise ValueError("Only read-only SELECT statements are allowed")
