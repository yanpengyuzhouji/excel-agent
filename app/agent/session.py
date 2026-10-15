"""Persisted owner-scoped sessions for global multi-workbook Agent runs."""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from ..models.agent import AgentConversationDao, AgentMessageDao  # type: ignore
from .react_loop import has_tool_protocol


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

    def add_ai(self, content: str, meta: Optional[Dict[str, Any]] = None) -> None:
        self._append("ai", content, meta)

    def get_budget_usage(self) -> Dict[str, int]:
        return next((m.get('meta') or {} for m in reversed(self.messages) if m['role'] == 'budget'), {})

    def add_budget_usage(self, usage: Dict[str, int]) -> None:
        self._append('budget', 'agent resource usage', usage)

    def add_context_checkpoint(self, snapshot):
        upto = max((m.get('index',-1) for m in self.messages),default=-1)
        self._append('context_checkpoint','compacted active context',{'snapshot':snapshot,'upto_index':upto})

    def add_context_usage(self, state):
        self._append('context','context window usage',state)

    def context_state(self):
        return next((m.get('meta') or {} for m in reversed(self.messages) if m['role']=='context'),{})

    def set_scope(self, scope: Dict[str, Any]) -> None:
        self.scope_type = scope['mode']
        self.scope_json = scope
        self._conv_dao.update(self.conv_id, self.owner_id, {
            'scope_type': self.scope_type, 'scope_json': json.dumps(scope, ensure_ascii=False),
        })

    def add_tool(self, action: str, observation: Any, detail: Optional[Dict[str, Any]] = None) -> None:
        meta: Dict[str, Any] = {"action": action, "observation": observation}
        if detail:
            meta["detail"] = {key: value for key, value in detail.items() if key != "thought"}
        self._append("tool", f"Action: {action}\nObservation: {observation}", meta)

    def history_messages(self) -> List[Dict[str, Any]]:
        checkpoint = next((m.get('meta') or {} for m in reversed(self.messages) if m['role']=='context_checkpoint'),{})
        cutoff = checkpoint.get('upto_index',-1)
        result = [dict(m) for m in checkpoint.get('snapshot',[]) if m.get('role') in {'user','assistant'} and isinstance(m.get('content'),str)]
        recent_rounds = sorted({m['round_index'] for m in self.messages})[-10:]
        completed_rounds = {m['round_index'] for m in self.messages if (m.get('meta') or {}).get('detail',{}).get('type') == 'step.done'}
        for message in self.messages:
            if message['round_index'] not in recent_rounds:
                continue
            role = message["role"]
            if message.get('index',0) <= cutoff and role != 'tool':
                continue
            if role == "user":
                result.append({"role": "user", "content": message["content"]})
            elif role == "ai" and not has_tool_protocol(message['content']):
                result.append({"role": "assistant", "content": message["content"]})
            elif role == "tool":
                detail = (message.get('meta') or {}).get('detail') or {}
                if detail.get('type') == 'catalog.candidates':
                    continue
                if message['round_index'] in completed_rounds and detail.get('type') in {'source.inspect','source.query.result','source.query.error','aggregate.result'}:
                    continue
                if detail.get('type') == 'step.done' and detail.get('action') == 'html_interpreter':
                    artifact = detail.get('result') or {}
                    content = json.dumps({k:v for k,v in artifact.items() if k != 'html'}, ensure_ascii=False)
                else:
                    content = json.dumps(detail,ensure_ascii=False) if detail else message['content']
                entry = {"role": "tool", "content": content}
                if detail.get('type') == 'source.query.result':
                    entry['evidence'] = detail
                elif detail.get('type') == 'step.done' and detail.get('action') in {'query_workbook','sql_query','verify_record_count'} and detail.get('status') == 'done':
                    entry['evidence'] = detail.get('result') or {}
                if message.get('index',0) <= cutoff:
                    if entry.get('evidence'):
                        entry['content'] = ''
                        result.append(entry)
                else:
                    result.append(entry)
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
