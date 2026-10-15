"""Workbook conversation persistence (session history).

Ported design from DB-GPT's StorageConversation / chat_history tables
(see docs/DBGPT-MEMORY-CLOSED-LOOP.md section 4): a conversation is a list of
typed messages under a conv_id, saved to SQLite.  Simplified to a single
`workbook_message` table keyed by (conv_id, index); conversation metadata
(workbook_id, title, message count, updated_at) lives in `workbook_conversation`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from ..db import BaseDao, Model


class WorkbookConversationEntity(Model):
    """One conversation (session) against a workbook."""

    __tablename__ = "workbook_conversation"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conv_id = Column(String(64), unique=True, nullable=False, index=True)
    workbook_id = Column(String(64), nullable=False, index=True)
    owner_id = Column(String(128), nullable=False, default="default", index=True)
    title = Column(String(512), nullable=False, default="")
    # The latest model context is kept on the session so the frontend can
    # restore it after a refresh.  Per-turn copies are also stored on user
    # messages by ExcelWorkbookConversation.add_user_message().
    model_name = Column(String(512), nullable=True)
    model_label = Column(String(512), nullable=True)
    model_id = Column(String(512), nullable=True)
    analysis_route = Column(String(32), nullable=True)
    analysis_reason = Column(String(128), nullable=True)
    model_size_b = Column(Integer, nullable=True)
    gmt_created = Column(DateTime, default=datetime.now)
    gmt_modified = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class WorkbookMessageEntity(Model):
    """One message inside a conversation (user/ai/tool/view)."""

    __tablename__ = "workbook_message"
    __table_args__ = (
        UniqueConstraint("conv_id", "index", name="uk_conv_index"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    conv_id = Column(String(64), nullable=False, index=True)
    index = Column(Integer, nullable=False)
    round_index = Column(Integer, nullable=False, default=1)
    role = Column(String(32), nullable=False)   # user | ai | tool | view
    content = Column(Text, nullable=False, default="")
    meta_json = Column(Text, nullable=True)     # optional JSON payload
    gmt_created = Column(DateTime, default=datetime.now)


class WorkbookConversationDao(BaseDao):
    def create_conversation(
        self,
        conv_id: str,
        workbook_id: str,
        title: str = "",
        owner_id: str = "default",
    ) -> WorkbookConversationEntity:
        entity = WorkbookConversationEntity(
            conv_id=conv_id,
            workbook_id=workbook_id,
            title=title,
            owner_id=owner_id,
        )
        with self.session() as session:
            session.add(entity)
            session.flush()
            session.refresh(entity)
            session.expunge(entity)
        return entity

    def get_by_conv_id(
        self,
        conv_id: str,
        workbook_id: Optional[str] = None,
        owner_id: Optional[str] = None,
    ) -> Optional[WorkbookConversationEntity]:
        with self.session() as session:
            query = session.query(WorkbookConversationEntity).filter(
                WorkbookConversationEntity.conv_id == conv_id
            )
            if workbook_id is not None:
                query = query.filter(
                    WorkbookConversationEntity.workbook_id == workbook_id
                )
            if owner_id is not None:
                query = query.filter(WorkbookConversationEntity.owner_id == owner_id)
            entity = query.first()
            if entity:
                session.expunge(entity)
            return entity

    def list_by_workbook(
        self,
        workbook_id: str,
        limit: int = 100,
        offset: int = 0,
        owner_id: Optional[str] = None,
    ) -> List[WorkbookConversationEntity]:
        with self.session() as session:
            query = session.query(WorkbookConversationEntity).filter(
                WorkbookConversationEntity.workbook_id == workbook_id
            )
            if owner_id is not None:
                query = query.filter(WorkbookConversationEntity.owner_id == owner_id)
            rows = (
                query.order_by(WorkbookConversationEntity.gmt_modified.desc())
                .offset(offset).limit(limit).all()
            )
            for row in rows:
                session.expunge(row)
            return list(rows)

    def touch(self, conv_id: str) -> None:
        with self.session() as session:
            session.query(WorkbookConversationEntity).filter(
                WorkbookConversationEntity.conv_id == conv_id
            ).update(
                {"gmt_modified": datetime.now()}
            )

    def update_model_context(
        self,
        conv_id: str,
        *,
        model_name: Optional[str] = None,
        model_label: Optional[str] = None,
        model_id: Optional[str] = None,
        analysis_route: Optional[str] = None,
        analysis_reason: Optional[str] = None,
        model_size_b: Optional[int] = None,
    ) -> None:
        values = {
            "model_name": model_name,
            "model_label": model_label,
            "model_id": model_id,
            "analysis_route": analysis_route,
            "analysis_reason": analysis_reason,
            "model_size_b": model_size_b,
            "gmt_modified": datetime.now(),
        }
        with self.session() as session:
            session.query(WorkbookConversationEntity).filter(
                WorkbookConversationEntity.conv_id == conv_id
            ).update(values)

    def delete_by_conv_id(self, conv_id: str) -> None:
        with self.session() as session:
            session.query(WorkbookConversationEntity).filter(
                WorkbookConversationEntity.conv_id == conv_id
            ).delete()

    def delete_by_workbook_id(self, workbook_id: str) -> None:
        with self.session() as session:
            session.query(WorkbookConversationEntity).filter(
                WorkbookConversationEntity.workbook_id == workbook_id
            ).delete()


class WorkbookMessageDao(BaseDao):
    def append_message(
        self,
        conv_id: str,
        index: int,
        round_index: int,
        role: str,
        content: str,
        meta_json: Optional[str] = None,
    ) -> None:
        entity = WorkbookMessageEntity(
            conv_id=conv_id,
            index=index,
            round_index=round_index,
            role=role,
            content=content,
            meta_json=meta_json,
        )
        with self.session() as session:
            session.add(entity)
            session.flush()

    def list_messages(self, conv_id: str) -> List[WorkbookMessageEntity]:
        with self.session() as session:
            rows = (
                session.query(WorkbookMessageEntity)
                .filter(WorkbookMessageEntity.conv_id == conv_id)
                .order_by(WorkbookMessageEntity.index.asc())
                .all()
            )
            for row in rows:
                session.expunge(row)
            return list(rows)

    def next_index(self, conv_id: str) -> int:
        with self.session() as session:
            row = (
                session.query(WorkbookMessageEntity.index)
                .filter(WorkbookMessageEntity.conv_id == conv_id)
                .order_by(WorkbookMessageEntity.index.desc())
                .first()
            )
            return (row[0] if row else -1) + 1

    def delete_by_conv_id(self, conv_id: str) -> None:
        with self.session() as session:
            session.query(WorkbookMessageEntity).filter(
                WorkbookMessageEntity.conv_id == conv_id
            ).delete()

    def delete_by_workbook_id(self, workbook_id: str) -> None:
        with self.session() as session:
            session.query(WorkbookMessageEntity).filter(
                WorkbookMessageEntity.conv_id.in_(
                    session.query(WorkbookConversationEntity.conv_id).filter(
                        WorkbookConversationEntity.workbook_id == workbook_id
                    )
                )
            ).delete(synchronize_session=False)
