"""Persisted owner-scoped sessions for global multi-workbook Agent runs."""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from ..models.agent import AgentConversationDao, AgentMessageDao  # type: ignore


class AgentConversation:
    """Small persistence wrapper mirroring the existing workbook conversation."""

    def __init__(self, entity: Any, conv_dao: AgentConversationDao, message_dao: Any):
        self.conv_id = entity.conv_id
        self.owner_id = entity.owner_id
        self.scope_type = entity.scope_type
        self.scope_json = json.loads(entity.scope_json or "{}")
        self.source_snapshot = json.loads(entity.source_snapshot_json or "[]")
        self.title = entity.title
        self.model_name = entity.model_name
        self.model_label = entity.model_label
        self.model_id = entity.model_id
        self._conv_dao = conv_dao
        self._message_dao = message_dao
        self.chat_order = 0
        self.messages: List[Dict[str, Any]] = []
        self._load_messages()

    @classmethod
    def create(
        cls,
        owner_id: str,
        scope_type: str,
        scope_json: Dict[str, Any],
        title: str = "",
        conv_id: Optional[str] = None,
        conv_dao: Optional[AgentConversationDao] = None,
        message_dao: Optional[Any] = None,
    ) -> "AgentConversation":
        conv_dao = conv_dao or AgentConversationDao()
        message_dao = message_dao or AgentMessageDao()
        conv_id = conv_id or uuid.uuid4().hex
        entity = conv_dao.create(
            {
                "conv_id": conv_id,
                "owner_id": owner_id,
                "scope_type": scope_type,
                "scope_json": json.dumps(scope_json, ensure_ascii=False),
                "source_snapshot_json": "[]",
                "title": title,
            }
        )
        return cls(entity, conv_dao, message_dao)

    @classmethod
    def load(
        cls,
        conv_id: str,
        owner_id: str,
        conv_dao: Optional[AgentConversationDao] = None,
        message_dao: Optional[Any] = None,
    ) -> Optional["AgentConversation"]:
        conv_dao = conv_dao or AgentConversationDao()
        message_dao = message_dao or AgentMessageDao()
        entity = conv_dao.get(conv_id, owner_id)
        return cls(entity, conv_dao, message_dao) if entity else None

    def _load_messages(self) -> None:
        rows = self._message_dao.list_messages(self.conv_id)
        self.messages = []
        for row in rows:
            try:
                meta = json.loads(row.meta_json) if row.meta_json else {}
            except (TypeError, ValueError):
                meta = {}
            self.messages.append(
                {
                    "index": row.index,
                    "round_index": row.round_index,
                    "role": row.role,
                    "content": row.content,
                    "meta": meta,
                }
            )
            self.chat_order = max(self.chat_order, int(row.round_index))

    def start_new_round(self) -> None:
        self.chat_order += 1

    def _append(self, role: str, content: str, meta: Optional[Dict[str, Any]] = None) -> None:
        index = self._message_dao.next_index(self.conv_id)
        self._message_dao.append_message(
            self.conv_id,
            index,
            self.chat_order,
            role,
            content,
            json.dumps(meta, ensure_ascii=False) if meta else None,
        )
        self.messages.append(
            {
                "index": index,
                "round_index": self.chat_order,
                "role": role,
                "content": content,
                "meta": meta or {},
            }
        )

    def add_user(self, content: str, meta: Optional[Dict[str, Any]] = None) -> None:
        self._append("user", content, meta)

    def add_ai(self, content: str) -> None:
        self._append("ai", content)

    def add_tool(self, action: str, observation: Any, detail: Optional[Dict[str, Any]] = None) -> None:
        meta: Dict[str, Any] = {"action": action, "observation": observation}
        if detail:
            meta["detail"] = {key: value for key, value in detail.items() if key != "thought"}
        self._append("tool", f"Action: {action}\nObservation: {observation}", meta)

    def history_messages(self) -> List[Dict[str, str]]:
        result = []
        for message in self.messages:
            role = message["role"]
            if role == "user":
                result.append({"role": "user", "content": message["content"]})
            elif role == "ai":
                result.append({"role": "assistant", "content": message["content"]})
            elif role == "tool":
                result.append({"role": "user", "content": message["content"]})
        return result

    def set_context(self, *, model_name: str, model_label: str, model_id: str) -> None:
        self.model_name = model_name
        self.model_label = model_label
        self.model_id = model_id
        self._conv_dao.update(
            self.conv_id,
            self.owner_id,
            {
                "model_name": model_name,
                "model_label": model_label,
                "model_id": model_id,
            },
        )

    def set_snapshot(self, snapshots: List[Dict[str, Any]]) -> None:
        self.source_snapshot = snapshots
        self._conv_dao.update(
            self.conv_id,
            self.owner_id,
            {"source_snapshot_json": json.dumps(snapshots, ensure_ascii=False)},
        )
