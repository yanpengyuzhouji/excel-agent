"""Replacement for dbgpt.storage.metadata (BaseDao/Model).

Provides a sqlalchemy declarative base, an engine pointing at the microservice's
own metadata SQLite DB (separate from each workbook's generated database.sqlite),
and a BaseDao mixin with a session() context manager.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import build_app_config

Model = declarative_base()

_engine = None
_SessionLocal = None


def _init_engine(storage_path: str):
    global _engine, _SessionLocal
    if _engine is not None:
        return
    meta_db = os.path.join(storage_path, "workbook_meta.db")
    os.makedirs(os.path.dirname(meta_db), exist_ok=True)
    _engine = create_engine(
        f"sqlite:///{meta_db}",
        connect_args={"check_same_thread": False},
    )
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)


def ensure_tables() -> None:
    if _engine is None:
        _init_engine(build_app_config().resolved_storage_path())
    Model.metadata.create_all(bind=_engine)
    # create_all does not add columns to an existing SQLite installation.
    # Assign legacy resources to the explicit `default` principal during the
    # one-way ownership migration; other principals cannot claim them.
    migrations = (
        ("workbook_dataset", "owner_id"),
        ("workbook_conversation", "owner_id"),
    )
    for table_name, column_name in migrations:
        columns = {item["name"] for item in inspect(_engine).get_columns(table_name)}
        if column_name not in columns:
            with _engine.begin() as connection:
                connection.execute(
                    text(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} "
                        "VARCHAR(128) NOT NULL DEFAULT 'default'"
                    )
                )

    # Persist the model and route used by the latest turn so the frontend can
    # restore the session context after a refresh.  Existing installations
    # receive these nullable columns without losing their conversation data.
    conversation_columns = {
        item["name"]
        for item in inspect(_engine).get_columns("workbook_conversation")
    }
    conversation_metadata_columns = {
        "model_name": "VARCHAR(512)",
        "model_label": "VARCHAR(512)",
        "model_id": "VARCHAR(512)",
        "analysis_route": "VARCHAR(32)",
        "analysis_reason": "VARCHAR(128)",
        "model_size_b": "INTEGER",
    }
    for column_name, column_type in conversation_metadata_columns.items():
        if column_name not in conversation_columns:
            with _engine.begin() as connection:
                connection.execute(
                    text(
                        f"ALTER TABLE workbook_conversation ADD COLUMN "
                        f"{column_name} {column_type}"
                    )
                )

    # Versions used to be allocated with SELECT MAX(version) + 1, which races
    # under concurrent uploads.  Backfill a per-workbook counter and keep the
    # import table protected by a database-level unique index.
    dataset_columns = {
        item["name"] for item in inspect(_engine).get_columns("workbook_dataset")
    }
    if "next_version" not in dataset_columns:
        with _engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE workbook_dataset ADD COLUMN next_version "
                    "INTEGER NOT NULL DEFAULT 1"
                )
            )
        dataset_columns.add("next_version")

    table_names = set(inspect(_engine).get_table_names())
    import_columns = (
        {item["name"] for item in inspect(_engine).get_columns("workbook_import")}
        if "workbook_import" in table_names
        else set()
    )
    if "workbook_id" in dataset_columns and {"workbook_id", "version"}.issubset(
        import_columns
    ):
        with _engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE workbook_dataset AS dataset "
                    "SET next_version = MAX(next_version, COALESCE(("
                    "SELECT MAX(imports.version) + 1 FROM workbook_import AS imports "
                    "WHERE imports.workbook_id = dataset.workbook_id"
                    "), 1))"
                )
            )
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "ux_workbook_import_workbook_version "
                    "ON workbook_import (workbook_id, version)"
                )
            )

    # One-way privacy cleanup for legacy tool messages. New writes never store
    # model Thought, while this migration removes the known historical fields
    # and the former canonical content prefix from existing business rows.
    if "workbook_message" in table_names:
        message_columns = {
            item["name"]
            for item in inspect(_engine).get_columns("workbook_message")
        }
        if {"role", "content", "meta_json"}.issubset(message_columns):
            with _engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE workbook_message "
                        "SET meta_json = json_remove(meta_json, '$.thought', "
                        "'$.detail.thought') "
                        "WHERE role = 'tool' AND json_valid(meta_json)"
                    )
                )
                connection.execute(
                    text(
                        "UPDATE workbook_message "
                        "SET content = substr(content, "
                        "instr(content, char(10) || 'Action:') + 1) "
                        "WHERE role = 'tool' "
                        "AND lower(ltrim(content)) LIKE 'thought:%' "
                        "AND instr(content, char(10) || 'Action:') > 0"
                    )
                )
        with _engine.begin() as connection:
            connection.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_{table_name}_{column_name} "
                    f"ON {table_name} ({column_name})"
                )
            )


class BaseDao:
    """Mixin providing a session() context manager (old-style session.query API)."""

    @contextmanager
    def session(self) -> Iterator:
        if _SessionLocal is None:
            _init_engine(build_app_config().resolved_storage_path())
        session = _SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


def get_engine():
    if _engine is None:
        _init_engine(build_app_config().resolved_storage_path())
    return _engine


def get_session():
    if _SessionLocal is None:
        _init_engine(build_app_config().resolved_storage_path())
    return _SessionLocal()
