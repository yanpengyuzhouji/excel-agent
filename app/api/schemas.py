"""Pydantic v2 request/response models, replacing
DB-GPT packages/dbgpt-serve/src/dbgpt_serve/workbook/api/schemas.py
(dbgtp._private.pydantic -> pydantic v2).
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

ImportStatus = Literal[
    "uploaded",
    "parsing",
    "draft",
    "validating",
    "ready",
    "updating",
    "committing",
    "active",
    "superseded",
    "failed",
]


class ValidationIssue(BaseModel):
    code: str
    level: Literal["error", "warning"]
    message: str
    sheet_id: Optional[str] = None
    row: Optional[int] = None
    column: Optional[str] = None
    count: Optional[int] = None


class WorkbookImportResponse(BaseModel):
    import_id: str
    workbook_id: str
    source_channel: Literal["chat_excel", "agent"]
    file_name: str
    version: int
    status: ImportStatus
    revision: int
    datasource_name: Optional[str] = None
    active_version: Optional[int] = None
    sheets: List[Dict[str, Any]] = Field(default_factory=list)
    validation: Dict[str, Any] = Field(default_factory=dict)
    error_message: Optional[str] = None


class DraftUpdateRequest(BaseModel):
    revision: int
    sheet_updates: List[Dict[str, Any]] = Field(default_factory=list)
    column_updates: List[Dict[str, Any]] = Field(default_factory=list)
    cell_updates: List[Dict[str, Any]] = Field(default_factory=list)


class ConfirmRequest(BaseModel):
    revision: int
    accepted_warning_codes: List[str] = Field(default_factory=list)


class ConfirmResponse(BaseModel):
    import_id: str
    workbook_id: str
    version: int
    status: Literal["active"]
    datasource_id: Optional[int] = None
    datasource_name: str
    database_path: str


class SQLQueryRequest(BaseModel):
    sql: str
    max_rows: int = Field(default=500, ge=1, le=1000)


class SQLQueryResponse(BaseModel):
    sql: str
    columns: List[str]
    rows: List[List[Any]]
    row_count: int
    truncated: bool = False
    elapsed_ms: float
    decimal_columns: List[str] = Field(default_factory=list)
    execution_sql: Optional[str] = None
    precision_scope: str = 'sqlite_native'


class ConversationSummary(BaseModel):
    conv_id: str
    workbook_id: str
    title: str = ""
    model_name: Optional[str] = None
    model_label: Optional[str] = None
    model_id: Optional[str] = None
    analysis_route: Optional[str] = None
    analysis_reason: Optional[str] = None
    model_size_b: Optional[int] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ConversationMessage(BaseModel):
    index: int
    round_index: int
    role: str
    content: str
    meta: Dict[str, Any] = Field(default_factory=dict)


class ConversationDetail(BaseModel):
    conv_id: str
    workbook_id: str
    model_name: Optional[str] = None
    model_label: Optional[str] = None
    model_id: Optional[str] = None
    analysis_route: Optional[str] = None
    analysis_reason: Optional[str] = None
    model_size_b: Optional[int] = None
    messages: List[ConversationMessage] = Field(default_factory=list)
