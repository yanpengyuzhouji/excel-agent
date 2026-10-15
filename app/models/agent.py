"""Persistence models for global Agent scopes, conversations, and relations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint

from ..db import BaseDao, Model


class DataSpaceEntity(Model):
    __tablename__ = "data_space"

    id = Column(Integer, primary_key=True, autoincrement=True)
    space_id = Column(String(64), unique=True, nullable=False, index=True)
    owner_id = Column(String(128), nullable=False, index=True)
    name = Column(String(512), nullable=False)
    description = Column(Text, nullable=False, default="")
    gmt_created = Column(DateTime, default=datetime.now)
    gmt_modified = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class DataSpaceWorkbookEntity(Model):
    __tablename__ = "data_space_workbook"
    __table_args__ = (
        UniqueConstraint("space_id", "workbook_id", name="uk_space_workbook"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    space_id = Column(String(64), nullable=False, index=True)
    workbook_id = Column(String(64), nullable=False, index=True)
    alias = Column(String(512), nullable=True)
    enabled = Column(Integer, nullable=False, default=1)


class AgentConversationEntity(Model):
    __tablename__ = "agent_conversation"

    id = Column(Integer, primary_key=True, autoincrement=True)
    conv_id = Column(String(64), unique=True, nullable=False, index=True)
    owner_id = Column(String(128), nullable=False, index=True)
    scope_type = Column(String(32), nullable=False, default="auto")
    scope_json = Column(Text, nullable=False, default="{}")
    source_snapshot_json = Column(Text, nullable=False, default="[]")
    title = Column(String(512), nullable=False, default="")
    pinned = Column(Integer, nullable=False, default=0, server_default='0')
    model_name = Column(String(512), nullable=True)
    model_label = Column(String(512), nullable=True)
    model_id = Column(String(512), nullable=True)
    gmt_created = Column(DateTime, default=datetime.now)
    gmt_modified = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class AgentMessageEntity(Model):
    __tablename__ = "agent_message"
    __table_args__ = (
        UniqueConstraint("conv_id", "index", name="uk_agent_conv_index"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    conv_id = Column(String(64), nullable=False, index=True)
    index = Column(Integer, nullable=False)
    round_index = Column(Integer, nullable=False, default=1)
    role = Column(String(32), nullable=False)
    content = Column(Text, nullable=False, default="")
    meta_json = Column(Text, nullable=True)
    gmt_created = Column(DateTime, default=datetime.now)


class DatasetRelationEntity(Model):
    __tablename__ = "dataset_relation"
    __table_args__ = (
        UniqueConstraint(
            "owner_id",
            "left_workbook_id",
            "left_table",
            "left_column",
            "right_workbook_id",
            "right_table",
            "right_column",
            name="uk_dataset_relation_columns",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    relation_id = Column(String(64), unique=True, nullable=False, index=True)
    owner_id = Column(String(128), nullable=False, index=True)
    left_workbook_id = Column(String(64), nullable=False)
    left_table = Column(String(256), nullable=False)
    left_column = Column(String(256), nullable=False)
    right_workbook_id = Column(String(64), nullable=False)
    right_table = Column(String(256), nullable=False)
    right_column = Column(String(256), nullable=False)
    relation_type = Column(String(64), nullable=False, default="equality")
    cardinality = Column(String(64), nullable=True)
    enabled = Column(Integer, nullable=False, default=1)
    confirmed_by = Column(String(128), nullable=True)
    gmt_created = Column(DateTime, default=datetime.now)
    gmt_modified = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class DataSpaceDao(BaseDao):
    def create(self, values: Dict[str, Any]) -> DataSpaceEntity:
        with self.session() as session:
            entity = DataSpaceEntity(**values)
            session.add(entity)
            session.flush()
            session.refresh(entity)
            session.expunge(entity)
            return entity

    def get(self, space_id: str, owner_id: str) -> Optional[DataSpaceEntity]:
        with self.session() as session:
            entity = (
                session.query(DataSpaceEntity)
                .filter(
                    DataSpaceEntity.space_id == space_id,
                    DataSpaceEntity.owner_id == owner_id,
                )
                .first()
            )
            if entity:
                session.expunge(entity)
            return entity

    def list(self, owner_id: str) -> List[DataSpaceEntity]:
        with self.session() as session:
            rows = (
                session.query(DataSpaceEntity)
                .filter(DataSpaceEntity.owner_id == owner_id)
                .order_by(DataSpaceEntity.gmt_modified.desc())
                .all()
            )
            for row in rows:
                session.expunge(row)
            return list(rows)

    def update(self, space_id: str, owner_id: str, values: Dict[str, Any]) -> bool:
        with self.session() as session:
            changed = (
                session.query(DataSpaceEntity)
                .filter(
                    DataSpaceEntity.space_id == space_id,
                    DataSpaceEntity.owner_id == owner_id,
                )
                .update({**values, "gmt_modified": datetime.now()})
            )
            return changed == 1

    def delete(self, space_id: str, owner_id: str) -> bool:
        with self.session() as session:
            owned = session.query(DataSpaceEntity.id).filter(
                DataSpaceEntity.space_id == space_id,
                DataSpaceEntity.owner_id == owner_id,
            ).first()
            if not owned:
                return False
            session.query(DataSpaceWorkbookEntity).filter(
                DataSpaceWorkbookEntity.space_id == space_id
            ).delete(synchronize_session=False)
            session.query(DataSpaceEntity).filter(
                DataSpaceEntity.space_id == space_id,
                DataSpaceEntity.owner_id == owner_id,
            ).delete(synchronize_session=False)
            return True

    def list_workbook_ids(self, space_id: str) -> List[str]:
        with self.session() as session:
            rows = (
                session.query(DataSpaceWorkbookEntity.workbook_id)
                .filter(
                    DataSpaceWorkbookEntity.space_id == space_id,
                    DataSpaceWorkbookEntity.enabled == 1,
                )
                .order_by(DataSpaceWorkbookEntity.id.asc())
                .all()
            )
            return [row[0] for row in rows]

    def replace_members(self, space_id: str, workbook_ids: List[str]) -> None:
        with self.session() as session:
            session.query(DataSpaceWorkbookEntity).filter(
                DataSpaceWorkbookEntity.space_id == space_id
            ).delete(synchronize_session=False)
            session.add_all(
                DataSpaceWorkbookEntity(space_id=space_id, workbook_id=workbook_id)
                for workbook_id in dict.fromkeys(workbook_ids)
            )


class AgentConversationDao(BaseDao):
    def create(self, values: Dict[str, Any]) -> AgentConversationEntity:
        with self.session() as session:
            entity = AgentConversationEntity(**values)
            session.add(entity)
            session.flush()
            session.refresh(entity)
            session.expunge(entity)
            return entity

    def get(self, conv_id: str, owner_id: str) -> Optional[AgentConversationEntity]:
        with self.session() as session:
            entity = (
                session.query(AgentConversationEntity)
                .filter(
                    AgentConversationEntity.conv_id == conv_id,
                    AgentConversationEntity.owner_id == owner_id,
                )
                .first()
            )
            if entity:
                session.expunge(entity)
            return entity

    def list(self, owner_id: str, limit: int = 100) -> List[AgentConversationEntity]:
        with self.session() as session:
            rows = (
                session.query(AgentConversationEntity)
                .filter(AgentConversationEntity.owner_id == owner_id)
                .order_by(AgentConversationEntity.pinned.desc(), AgentConversationEntity.gmt_modified.desc())
                .limit(max(1, min(int(limit), 500)))
                .all()
            )
            for row in rows:
                session.expunge(row)
            return list(rows)

    def update(self, conv_id: str, owner_id: str, values: Dict[str, Any]) -> None:
        with self.session() as session:
            session.query(AgentConversationEntity).filter(
                AgentConversationEntity.conv_id == conv_id,
                AgentConversationEntity.owner_id == owner_id,
            ).update({**values, "gmt_modified": datetime.now()})

    def delete(self, conv_id: str, owner_id: str) -> None:
        with self.session() as session:
            owned = session.query(AgentConversationEntity.id).filter(
                AgentConversationEntity.conv_id == conv_id,
                AgentConversationEntity.owner_id == owner_id,
            ).first()
            if not owned:
                return
            session.query(AgentMessageEntity).filter(
                AgentMessageEntity.conv_id == conv_id
            ).delete(synchronize_session=False)
            session.query(AgentConversationEntity).filter(
                AgentConversationEntity.conv_id == conv_id,
                AgentConversationEntity.owner_id == owner_id,
            ).delete(synchronize_session=False)

    def next_index(self, conv_id: str) -> int:
        with self.session() as session:
            row = (
                session.query(AgentMessageEntity.index)
                .filter(AgentMessageEntity.conv_id == conv_id)
                .order_by(AgentMessageEntity.index.desc())
                .first()
            )
            return int(row[0]) + 1 if row else 1

    def append_message(
        self,
        conv_id: str,
        index: int,
        round_index: int,
        role: str,
        content: str,
        meta_json: Optional[str] = None,
    ) -> None:
        with self.session() as session:
            session.add(
                AgentMessageEntity(
                    conv_id=conv_id,
                    index=index,
                    round_index=round_index,
                    role=role,
                    content=content,
                    meta_json=meta_json,
                )
            )

    def list_messages(self, conv_id: str) -> List[AgentMessageEntity]:
        with self.session() as session:
            rows = (
                session.query(AgentMessageEntity)
                .filter(AgentMessageEntity.conv_id == conv_id)
                .order_by(AgentMessageEntity.index.asc())
                .all()
            )
            for row in rows:
                session.expunge(row)
            return list(rows)


class DatasetRelationDao(BaseDao):
    def create(self, values: Dict[str, Any]) -> DatasetRelationEntity:
        with self.session() as session:
            entity = DatasetRelationEntity(**values)
            session.add(entity)
            session.flush()
            session.refresh(entity)
            session.expunge(entity)
            return entity

    def get(self, relation_id: str, owner_id: str) -> Optional[DatasetRelationEntity]:
        with self.session() as session:
            entity = (
                session.query(DatasetRelationEntity)
                .filter(
                    DatasetRelationEntity.relation_id == relation_id,
                    DatasetRelationEntity.owner_id == owner_id,
                )
                .first()
            )
            if entity:
                session.expunge(entity)
            return entity

    def list(self, owner_id: str, workbook_ids: Optional[List[str]] = None) -> List[DatasetRelationEntity]:
        with self.session() as session:
            query = session.query(DatasetRelationEntity).filter(
                DatasetRelationEntity.owner_id == owner_id,
                DatasetRelationEntity.enabled == 1,
            )
            if workbook_ids:
                query = query.filter(
                    (DatasetRelationEntity.left_workbook_id.in_(workbook_ids))
                    | (DatasetRelationEntity.right_workbook_id.in_(workbook_ids))
                )
            rows = query.order_by(DatasetRelationEntity.gmt_modified.desc()).all()
            for row in rows:
                session.expunge(row)
            return list(rows)

    def delete(self, relation_id: str, owner_id: str) -> bool:
        with self.session() as session:
            changed = session.query(DatasetRelationEntity).filter(
                DatasetRelationEntity.relation_id == relation_id,
                DatasetRelationEntity.owner_id == owner_id,
            ).delete(synchronize_session=False)
            return changed == 1


# Message persistence is intentionally kept on the conversation DAO so both
# rows use the same metadata transaction/session implementation.
AgentMessageDao = AgentConversationDao
