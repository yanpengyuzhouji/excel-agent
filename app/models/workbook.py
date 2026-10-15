"""SQLAlchemy entities and owner-scoped DAOs for workbook persistence."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint, text

from ..db import BaseDao, Model


class WorkbookDatasetEntity(Model):
    """Logical workbook whose imports form an auditable version history."""

    __tablename__ = "workbook_dataset"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workbook_id = Column(String(64), unique=True, nullable=False, index=True)
    owner_id = Column(String(128), nullable=False, default="default", index=True)
    display_name = Column(String(512), nullable=False)
    datasource_name = Column(String(255), unique=True, nullable=False)
    active_version = Column(Integer, nullable=True)
    active_import_id = Column(String(64), nullable=True)
    # Allocated with one atomic UPDATE ... RETURNING statement.  Gaps are
    # acceptable (for example when parsing fails); duplicate versions are not.
    next_version = Column(Integer, nullable=False, default=1)
    gmt_created = Column(DateTime, default=datetime.now)
    gmt_modified = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class WorkbookImportEntity(Model):
    """One immutable source version plus its mutable pre-commit review draft."""

    __tablename__ = "workbook_import"
    __table_args__ = (
        UniqueConstraint("workbook_id", "version", name="uk_workbook_version"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    import_id = Column(String(64), unique=True, nullable=False, index=True)
    workbook_id = Column(String(64), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    source_channel = Column(String(32), nullable=False)
    file_name = Column(String(512), nullable=False)
    source_hash = Column(String(128), nullable=False)
    source_path = Column(Text, nullable=False)
    draft_path = Column(Text, nullable=False)
    database_path = Column(Text, nullable=True)
    status = Column(String(32), nullable=False, default="uploaded", index=True)
    revision = Column(Integer, nullable=False, default=1)
    summary_json = Column(Text, nullable=True)
    validation_json = Column(Text, nullable=True)
    accepted_warnings_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    gmt_created = Column(DateTime, default=datetime.now)
    gmt_modified = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class WorkbookDatasetDao(BaseDao):
    def list_by_owner(
        self, owner_id: str, limit: int = 200, offset: int = 0
    ) -> list[WorkbookDatasetEntity]:
        with self.session() as session:
            rows = (
                session.query(WorkbookDatasetEntity)
                .filter(WorkbookDatasetEntity.owner_id == owner_id)
                .order_by(WorkbookDatasetEntity.gmt_modified.desc())
                .offset(max(0, int(offset)))
                .limit(max(1, min(int(limit), 1000)))
                .all()
            )
            for row in rows:
                session.expunge(row)
            return list(rows)

    def get_by_workbook_id(
        self, workbook_id: str, owner_id: Optional[str] = None
    ) -> Optional[WorkbookDatasetEntity]:
        with self.session() as session:
            query = session.query(WorkbookDatasetEntity).filter(
                WorkbookDatasetEntity.workbook_id == workbook_id
            )
            if owner_id is not None:
                query = query.filter(WorkbookDatasetEntity.owner_id == owner_id)
            entity = query.first()
            if entity:
                session.expunge(entity)
            return entity

    def create_entity(self, values: Dict[str, Any]) -> WorkbookDatasetEntity:
        entity = WorkbookDatasetEntity(**values)
        with self.session() as session:
            session.add(entity)
            session.flush()
            session.refresh(entity)
            session.expunge(entity)
        return entity

    def update_entity(self, workbook_id: str, values: Dict[str, Any]) -> None:
        with self.session() as session:
            session.query(WorkbookDatasetEntity).filter(
                WorkbookDatasetEntity.workbook_id == workbook_id
            ).update(values)

    def allocate_next_version(self, workbook_id: str) -> int:
        """Atomically reserve and return the next import version."""

        with self.session() as session:
            version = session.execute(
                text(
                    "UPDATE workbook_dataset "
                    "SET next_version = next_version + 1 "
                    "WHERE workbook_id = :workbook_id "
                    "RETURNING next_version - 1"
                ),
                {"workbook_id": workbook_id},
            ).scalar_one_or_none()
            if version is None:
                raise ValueError(f"Workbook not found: {workbook_id}")
            return int(version)

    def delete_by_workbook_id(self, workbook_id: str) -> None:
        with self.session() as session:
            session.query(WorkbookDatasetEntity).filter(
                WorkbookDatasetEntity.workbook_id == workbook_id
            ).delete()


class WorkbookImportDao(BaseDao):
    def get_by_import_id(self, import_id: str) -> Optional[WorkbookImportEntity]:
        with self.session() as session:
            entity = (
                session.query(WorkbookImportEntity)
                .filter(WorkbookImportEntity.import_id == import_id)
                .first()
            )
            if entity:
                session.expunge(entity)
            return entity

    def list_imports(
        self,
        status: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
        owner_id: Optional[str] = None,
    ) -> list[WorkbookImportEntity]:
        with self.session() as session:
            query = session.query(WorkbookImportEntity).order_by(
                WorkbookImportEntity.gmt_created.desc()
            )
            if owner_id is not None:
                query = query.join(
                    WorkbookDatasetEntity,
                    WorkbookDatasetEntity.workbook_id
                    == WorkbookImportEntity.workbook_id,
                ).filter(WorkbookDatasetEntity.owner_id == owner_id)
            if status:
                query = query.filter(WorkbookImportEntity.status == status)
            rows = query.offset(offset).limit(limit).all()
            for row in rows:
                session.expunge(row)
            return list(rows)

    def create_entity(self, values: Dict[str, Any]) -> WorkbookImportEntity:
        entity = WorkbookImportEntity(**values)
        with self.session() as session:
            session.add(entity)
            session.flush()
            session.refresh(entity)
            session.expunge(entity)
        return entity

    def update_entity(self, import_id: str, values: Dict[str, Any]) -> None:
        with self.session() as session:
            session.query(WorkbookImportEntity).filter(
                WorkbookImportEntity.import_id == import_id
            ).update(values)

    def claim_draft_revision(
        self, import_id: str, expected_revision: int, expected_status: str
    ) -> bool:
        """Exclusively claim one editable revision without advancing it."""

        with self.session() as session:
            changed = (
                session.query(WorkbookImportEntity)
                .filter(
                    WorkbookImportEntity.import_id == import_id,
                    WorkbookImportEntity.revision == expected_revision,
                    WorkbookImportEntity.status == expected_status,
                )
                .update({"status": "updating"}, synchronize_session=False)
            )
            return changed == 1

    def complete_draft_revision(
        self, import_id: str, expected_revision: int, values: Dict[str, Any]
    ) -> bool:
        """Commit a claim only when revision and claim state still match."""

        update_values = dict(values)
        update_values["revision"] = expected_revision + 1
        with self.session() as session:
            changed = (
                session.query(WorkbookImportEntity)
                .filter(
                    WorkbookImportEntity.import_id == import_id,
                    WorkbookImportEntity.revision == expected_revision,
                    WorkbookImportEntity.status == "updating",
                )
                .update(update_values, synchronize_session=False)
            )
            return changed == 1

    def release_draft_revision(
        self, import_id: str, expected_revision: int, previous_status: str
    ) -> bool:
        """Release a failed claim without advancing the visible revision."""

        with self.session() as session:
            changed = (
                session.query(WorkbookImportEntity)
                .filter(
                    WorkbookImportEntity.import_id == import_id,
                    WorkbookImportEntity.revision == expected_revision,
                    WorkbookImportEntity.status == "updating",
                )
                .update({"status": previous_status}, synchronize_session=False)
            )
            return changed == 1

    def mark_other_versions_superseded(
        self, workbook_id: str, active_import_id: str
    ) -> None:
        with self.session() as session:
            session.query(WorkbookImportEntity).filter(
                WorkbookImportEntity.workbook_id == workbook_id,
                WorkbookImportEntity.import_id != active_import_id,
                WorkbookImportEntity.status == "active",
            ).update({"status": "superseded"})

    def delete_by_workbook_id(self, workbook_id: str) -> None:
        with self.session() as session:
            session.query(WorkbookImportEntity).filter(
                WorkbookImportEntity.workbook_id == workbook_id
            ).delete()
