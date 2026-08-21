"""Excel workbook conversation manager (session history + multi-turn).

Ports DB-GPT's StorageConversation lifecycle (see
docs/DBGPT-MEMORY-CLOSED-LOOP.md section 3/6):

    start_new_round -> add_user_message -> [LLM] -> add_ai_message
    -> add_tool_message -> end_current_round -> save_to_storage

Here "save" is implicit: every append writes through to SQLite immediately, so
there is no separate commit step.  The conversation is reconstructed from the
DB on load, which gives multi-turn memory across requests and server restarts.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

from .models.conversation import (
    WorkbookConversationDao,
    WorkbookMessageDao,
)

logger = logging.getLogger(__name__)


class ExcelWorkbookConversation:
    """A persisted conversation against one workbook."""

    # Roles we persist (matches DB-GPT BaseMessage types).
    ROLE_USER = "user"
    ROLE_AI = "ai"
    ROLE_TOOL = "tool"
    ROLE_VIEW = "view"
    ROLE_BUDGET = "budget"

    def __init__(
        self,
        conv_id: str,
        workbook_id: str,
        owner_id: str = "default",
        conv_dao: Optional[WorkbookConversationDao] = None,
        message_dao: Optional[WorkbookMessageDao] = None,
        model_name: Optional[str] = None,
        model_label: Optional[str] = None,
        model_id: Optional[str] = None,
        analysis_route: Optional[str] = None,
        analysis_reason: Optional[str] = None,
        model_size_b: Optional[int] = None,
    ):
        self.conv_id = conv_id
        self.workbook_id = workbook_id
        self.owner_id = owner_id
        self._conv_dao = conv_dao or WorkbookConversationDao()
        self._message_dao = message_dao or WorkbookMessageDao()
        self.model_name = model_name
        self.model_label = model_label
        self.model_id = model_id
        self.analysis_route = analysis_route
        self.analysis_reason = analysis_reason
        self.model_size_b = model_size_b
        self.chat_order = 1
        self.messages: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ factory

    @classmethod
    def create(
        cls,
        workbook_id: str,
        title: str = "",
        conv_id: Optional[str] = None,
        conv_dao: Optional[WorkbookConversationDao] = None,
        message_dao: Optional[WorkbookMessageDao] = None,
        owner_id: str = "default",
    ) -> "ExcelWorkbookConversation":
        """Create a fresh conversation (or load an existing conv_id)."""
        conv_dao = conv_dao or WorkbookConversationDao()
        message_dao = message_dao or WorkbookMessageDao()
        if conv_id and conv_dao.get_by_conv_id(
            conv_id, workbook_id=workbook_id, owner_id=owner_id
        ):
            return cls.load(
                conv_id, workbook_id, conv_dao, message_dao, owner_id=owner_id
            )
        conv_id = conv_id or uuid.uuid4().hex
        conv_dao.create_conversation(
            conv_id, workbook_id, title=title, owner_id=owner_id
        )
        conv = cls(conv_id, workbook_id, owner_id, conv_dao, message_dao)
        return conv

    @classmethod
    def load(
        cls,
        conv_id: str,
        workbook_id: str,
        conv_dao: Optional[WorkbookConversationDao] = None,
        message_dao: Optional[WorkbookMessageDao] = None,
        owner_id: str = "default",
    ) -> Optional["ExcelWorkbookConversation"]:
        """Load an existing conversation; None if conv_id unknown."""
        conv_dao = conv_dao or WorkbookConversationDao()
        message_dao = message_dao or WorkbookMessageDao()
        entity = conv_dao.get_by_conv_id(
            conv_id, workbook_id=workbook_id, owner_id=owner_id
        )
        if not entity:
            return None
        conv = cls(
            conv_id,
            workbook_id,
            owner_id,
            conv_dao,
            message_dao,
            model_name=getattr(entity, "model_name", None),
            model_label=getattr(entity, "model_label", None),
            model_id=getattr(entity, "model_id", None),
            analysis_route=getattr(entity, "analysis_route", None),
            analysis_reason=getattr(entity, "analysis_reason", None),
            model_size_b=getattr(entity, "model_size_b", None),
        )
        conv._load_messages()
        return conv

    # ------------------------------------------------------------------ rounds

    def start_new_round(self) -> None:
        self.chat_order += 1

    # ------------------------------------------------------------------ append

    def _append(self, role: str, content: str, meta: Optional[dict] = None) -> None:
        index = self._message_dao.next_index(self.conv_id)
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        self._message_dao.append_message(
            self.conv_id, index, self.chat_order, role, content, meta_json
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
        self._conv_dao.touch(self.conv_id)

    def add_user_message(self, text: str, meta: Optional[dict] = None) -> None:
        self._append(self.ROLE_USER, text, meta=meta)

    def add_ai_message(self, text: str) -> None:
        self._append(self.ROLE_AI, text)

    def add_tool_message(
        self,
        action: str,
        observation: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist an auditable action/result pair, never private reasoning."""

        content = f"Action: {action}\nObservation: {observation}".strip()
        meta: Dict[str, Any] = {
            "action": action,
            "observation": observation,
        }
        if detail:
            safe_detail = dict(detail)
            safe_detail.pop("thought", None)
            meta["detail"] = safe_detail
        self._append(self.ROLE_TOOL, content, meta=meta)

    def add_view_message(self, content: str) -> None:
        self._append(self.ROLE_VIEW, content)

    def add_budget_usage(self, usage: Dict[str, int]) -> None:
        self._append(self.ROLE_BUDGET, "agent resource usage", meta=usage)

    def set_model_context(
        self,
        *,
        model_name: Optional[str],
        model_label: Optional[str],
        model_id: Optional[str],
        analysis_route: Optional[str],
        analysis_reason: Optional[str],
        model_size_b: Optional[int],
    ) -> None:
        """Persist the latest model context for refresh-safe session restore."""
        self.model_name = model_name
        self.model_label = model_label
        self.model_id = model_id
        self.analysis_route = analysis_route
        self.analysis_reason = analysis_reason
        self.model_size_b = model_size_b
        self._conv_dao.update_model_context(
            self.conv_id,
            model_name=model_name,
            model_label=model_label,
            model_id=model_id,
            analysis_route=analysis_route,
            analysis_reason=analysis_reason,
            model_size_b=model_size_b,
        )

    def model_context(self) -> Dict[str, Any]:
        return {
            "model_name": self.model_name,
            "model_label": self.model_label,
            "model_id": self.model_id,
            "analysis_route": self.analysis_route,
            "analysis_reason": self.analysis_reason,
            "model_size_b": self.model_size_b,
        }

    def get_budget_usage(self) -> Dict[str, int]:
        for message in reversed(self.messages):
            if message.get("role") == self.ROLE_BUDGET:
                return {
                    key: int((message.get("meta") or {}).get(key, 0))
                    for key in (
                        "llm_calls", "tool_calls", "sql_calls",
                        "estimated_tokens",
                    )
                }
        return {}

    # ------------------------------------------------------------------ read

    def _load_messages(self) -> None:
        self.messages = []
        for row in self._message_dao.list_messages(self.conv_id):
            try:
                meta = json.loads(row.meta_json) if row.meta_json else {}
            except Exception:
                meta = {}
            if row.role == self.ROLE_TOOL:
                meta.pop("thought", None)
                if isinstance(meta.get("detail"), dict):
                    meta["detail"].pop("thought", None)
                content = re.sub(
                    r"\A\s*Thought\s*:.*?\n\s*(?=Action\s*:)",
                    "",
                    row.content,
                    flags=re.DOTALL | re.IGNORECASE,
                )
            else:
                content = row.content
            self.messages.append(
                {
                    "index": row.index,
                    "round_index": row.round_index,
                    "role": row.role,
                    "content": content,
                    "meta": meta,
                }
            )
        # chat_order = max round_index (or 1)
        if self.messages:
            self.chat_order = max(m["round_index"] for m in self.messages)

    def get_history_messages(
        self, keep_end_rounds: int = 10
    ) -> List[Dict[str, Any]]:
        """Messages formatted for LLM context (user/ai/tool), windowed to
        recent `keep_end_rounds` rounds.  view messages are excluded."""
        if keep_end_rounds > 0:
            rounds = sorted({m["round_index"] for m in self.messages})
            recent = rounds[-keep_end_rounds:]
            windowed = [m for m in self.messages if m["round_index"] in recent]
        else:
            windowed = self.messages
        return [
            m
            for m in windowed
            if m["role"] in (self.ROLE_USER, self.ROLE_AI, self.ROLE_TOOL)
        ]

    # ------------------------------------------------------------------ delete

    def delete(self) -> None:
        self._message_dao.delete_by_conv_id(self.conv_id)
        self._conv_dao.delete_by_conv_id(self.conv_id)
        self.messages = []

    @staticmethod
    def delete_by_workbook_id(workbook_id: str) -> None:
        WorkbookMessageDao().delete_by_workbook_id(workbook_id)
        WorkbookConversationDao().delete_by_workbook_id(workbook_id)

    @staticmethod
    def list_workbook_conversations(
        workbook_id: str, owner_id: str = "default"
    ) -> List[dict]:
        dao = WorkbookConversationDao()
        rows = dao.list_by_workbook(workbook_id, owner_id=owner_id)
        return [
            {
                "conv_id": r.conv_id,
                "workbook_id": r.workbook_id,
                "title": r.title,
                "model_name": getattr(r, "model_name", None),
                "model_label": getattr(r, "model_label", None),
                "model_id": getattr(r, "model_id", None),
                "analysis_route": getattr(r, "analysis_route", None),
                "analysis_reason": getattr(r, "analysis_reason", None),
                "model_size_b": getattr(r, "model_size_b", None),
                "created_at": r.gmt_created.isoformat() if r.gmt_created else None,
                "updated_at": r.gmt_modified.isoformat() if r.gmt_modified else None,
            }
            for r in rows
        ]
