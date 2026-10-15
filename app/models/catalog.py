"""Owner-scoped catalog models for multi-workbook Agent routing."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Dict, List, Optional

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint, text

from ..db import BaseDao, Model


class WorkbookCatalogEntity(Model):
    """Searchable schema summary for one active workbook version.

    The catalog intentionally contains metadata only.  Business rows remain in
    each workbook's isolated SQLite database and are read only after a source
    has been selected and authorized for the current Agent run.
    """

    __tablename__ = "workbook_catalog"
    __table_args__ = (
        UniqueConstraint("workbook_id", name="uk_workbook_catalog_workbook"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    workbook_id = Column(String(64), nullable=False, unique=True, index=True)
    owner_id = Column(String(128), nullable=False, index=True)
    active_import_id = Column(String(64), nullable=False, index=True)
    active_version = Column(Integer, nullable=False)
    display_name = Column(String(512), nullable=False, default="")
    source_file = Column(String(512), nullable=True)
    schema_digest = Column(String(128), nullable=False)
    catalog_text = Column(Text, nullable=False, default="")
    catalog_json = Column(Text, nullable=False, default="{}")
    status = Column(String(32), nullable=False, default="ready", index=True)
    error_message = Column(Text, nullable=True)
    gmt_created = Column(DateTime, default=datetime.now)
    gmt_modified = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class WorkbookCatalogDao(BaseDao):
    def get_by_workbook_id(
        self, workbook_id: str, owner_id: Optional[str] = None
    ) -> Optional[WorkbookCatalogEntity]:
        with self.session() as session:
            query = session.query(WorkbookCatalogEntity).filter(
                WorkbookCatalogEntity.workbook_id == workbook_id
            )
            if owner_id is not None:
                query = query.filter(WorkbookCatalogEntity.owner_id == owner_id)
            entity = query.first()
            if entity:
                session.expunge(entity)
            return entity

    def list_by_owner(
        self,
        owner_id: str,
        *,
        status: str = "ready",
        limit: int = 200,
        offset: int = 0,
    ) -> List[WorkbookCatalogEntity]:
        with self.session() as session:
            rows = (
                session.query(WorkbookCatalogEntity)
                .filter(
                    WorkbookCatalogEntity.owner_id == owner_id,
                    WorkbookCatalogEntity.status == status,
                )
                .order_by(WorkbookCatalogEntity.gmt_modified.desc())
                .offset(max(0, int(offset)))
                .limit(max(1, min(int(limit), 1000)))
                .all()
            )
            for row in rows:
                session.expunge(row)
            return list(rows)

    def upsert(self, values: Dict[str, Any]) -> WorkbookCatalogEntity:
        workbook_id = str(values["workbook_id"])
        with self.session() as session:
            entity = (
                session.query(WorkbookCatalogEntity)
                .filter(WorkbookCatalogEntity.workbook_id == workbook_id)
                .first()
            )
            if entity is None:
                entity = WorkbookCatalogEntity(**values)
                session.add(entity)
            else:
                for key, value in values.items():
                    setattr(entity, key, value)
                entity.gmt_modified = datetime.now()
            session.flush()
            try:
                session.execute(
                    text(
                        "DELETE FROM workbook_catalog_fts WHERE workbook_id = :workbook_id"
                    ),
                    {"workbook_id": workbook_id},
                )
                session.execute(
                    text(
                        "INSERT INTO workbook_catalog_fts "
                        "(workbook_id, owner_id, catalog_text) "
                        "VALUES (:workbook_id, :owner_id, :catalog_text)"
                    ),
                    {
                        "workbook_id": workbook_id,
                        "owner_id": values.get("owner_id", ""),
                        "catalog_text": values.get("catalog_text", ""),
                    },
                )
            except Exception:
                # FTS5 is an optional acceleration only.
                pass
            session.refresh(entity)
            session.expunge(entity)
            return entity

    def mark_stale(self, workbook_id: str, owner_id: Optional[str] = None) -> None:
        with self.session() as session:
            query = session.query(WorkbookCatalogEntity).filter(
                WorkbookCatalogEntity.workbook_id == workbook_id
            )
            if owner_id is not None:
                query = query.filter(WorkbookCatalogEntity.owner_id == owner_id)
            query.update({"status": "stale", "gmt_modified": datetime.now()})

    def delete_by_workbook_id(self, workbook_id: str) -> None:
        with self.session() as session:
            session.query(WorkbookCatalogEntity).filter(
                WorkbookCatalogEntity.workbook_id == workbook_id
            ).delete()
            try:
                session.execute(
                    text(
                        "DELETE FROM workbook_catalog_fts WHERE workbook_id = :workbook_id"
                    ),
                    {"workbook_id": workbook_id},
                )
            except Exception:
                pass

    def search_by_owner(
        self, owner_id: str, query: str, *, limit: int = 20
    ) -> List[WorkbookCatalogEntity]:
        """FTS5 search with owner filtering applied inside the SQL query."""

        terms = re.findall(r"[A-Za-z0-9_]+|[\u3400-\u9fff]{2,}", str(query or ""))
        if not terms:
            return []
        match = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms[:20])
        with self.session() as session:
            try:
                ids = session.execute(
                    text(
                        "SELECT workbook_id FROM workbook_catalog_fts "
                        "WHERE workbook_catalog_fts MATCH :match AND owner_id = :owner "
                        "LIMIT :limit"
                    ),
                    {"match": match, "owner": owner_id, "limit": max(1, min(int(limit), 100))},
                ).scalars().all()
            except Exception:
                return []
            if not ids:
                return []
            rows = (
                session.query(WorkbookCatalogEntity)
                .filter(
                    WorkbookCatalogEntity.owner_id == owner_id,
                    WorkbookCatalogEntity.workbook_id.in_(list(ids)),
                )
                .all()
            )
            by_id = {row.workbook_id: row for row in rows}
            ordered = [by_id[item] for item in ids if item in by_id]
            for row in ordered:
                session.expunge(row)
            return ordered
