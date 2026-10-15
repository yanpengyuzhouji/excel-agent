"""Hard per-run resource budgets for the Excel ReAct workflow."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class AgentBudget:
    max_llm_calls: int = 12
    max_tool_calls: int = 12
    max_sql_calls: int = 8
    max_total_tokens: int = 100_000
    max_prompt_chars: int = 300_000
    max_seconds: float = 300.0
    max_conversation_llm_calls: int = 100
    max_conversation_tool_calls: int = 100
    max_conversation_sql_calls: int = 50
    max_conversation_tokens: int = 500_000
    enforce_conversation_token_limit: bool = True
    llm_calls: int = 0
    tool_calls: int = 0
    sql_calls: int = 0
    estimated_tokens: int = 0
    conversation_llm_calls: int = 0
    conversation_tool_calls: int = 0
    conversation_sql_calls: int = 0
    conversation_tokens: int = 0
    started: float = field(default_factory=time.monotonic)

    @classmethod
    def from_config(cls, config: Dict[str, Any] | None,
                    prior: Dict[str, Any] | None = None) -> "AgentBudget":
        config = config or {}
        prior = prior or {}
        values = {
            field: config.get(field, getattr(cls(), field))
            for field in (
                "max_llm_calls", "max_tool_calls", "max_sql_calls",
                "max_total_tokens", "max_prompt_chars", "max_seconds",
                "max_conversation_llm_calls", "max_conversation_tool_calls",
                "max_conversation_sql_calls", "max_conversation_tokens",
                "enforce_conversation_token_limit",
            )
        }
        values.update({
            "conversation_llm_calls": int(prior.get("llm_calls", 0)),
            "conversation_tool_calls": int(prior.get("tool_calls", 0)),
            "conversation_sql_calls": int(prior.get("sql_calls", 0)),
            "conversation_tokens": int(prior.get("estimated_tokens", 0)),
        })
        return cls(**values)

    def _check_time(self) -> None:
        if time.monotonic() - self.started >= float(self.max_seconds):
            raise BudgetExceeded(
                f"Agent time budget exceeded ({self.max_seconds} seconds)"
            )

    @staticmethod
    def _chars(messages: Iterable[Dict[str, Any]]) -> int:
        return sum(len(str(item.get("content", ""))) for item in messages)

    def before_llm(self, messages: Iterable[Dict[str, Any]]) -> None:
        self._check_time()
        prompt_chars = self._chars(messages)
        if prompt_chars > int(self.max_prompt_chars):
            raise BudgetExceeded(
                f"Agent prompt budget exceeded ({self.max_prompt_chars} characters)"
            )
        if self.llm_calls >= int(self.max_llm_calls):
            raise BudgetExceeded(
                f"Agent LLM-call budget exceeded ({self.max_llm_calls})"
            )
        if self.conversation_llm_calls >= int(self.max_conversation_llm_calls):
            raise BudgetExceeded(
                "Conversation LLM-call budget exceeded "
                f"({self.max_conversation_llm_calls})"
            )
        estimated = math.ceil(prompt_chars / 4)
        if self.estimated_tokens + estimated > int(self.max_total_tokens):
            raise BudgetExceeded(
                f"Agent token budget exceeded ({self.max_total_tokens})"
            )
        if self.enforce_conversation_token_limit and self.conversation_tokens + estimated > int(self.max_conversation_tokens):
            raise BudgetExceeded(
                "Conversation token budget exceeded "
                f"({self.max_conversation_tokens})"
            )
        self.llm_calls += 1
        self.conversation_llm_calls += 1
        self.estimated_tokens += estimated
        self.conversation_tokens += estimated

    def after_llm(self, reply: str) -> None:
        reply_tokens = math.ceil(len(reply or "") / 4)
        self.estimated_tokens += reply_tokens
        self.conversation_tokens += reply_tokens
        if self.estimated_tokens > int(self.max_total_tokens):
            raise BudgetExceeded(
                f"Agent token budget exceeded ({self.max_total_tokens})"
            )
        if self.enforce_conversation_token_limit and self.conversation_tokens > int(self.max_conversation_tokens):
            raise BudgetExceeded(
                "Conversation token budget exceeded "
                f"({self.max_conversation_tokens})"
            )

    def before_tool(self, action: str) -> None:
        self._check_time()
        if self.tool_calls >= int(self.max_tool_calls):
            raise BudgetExceeded(
                f"Agent tool-call budget exceeded ({self.max_tool_calls})"
            )
        if self.conversation_tool_calls >= int(self.max_conversation_tool_calls):
            raise BudgetExceeded(
                "Conversation tool-call budget exceeded "
                f"({self.max_conversation_tool_calls})"
            )
        if action == "sql_query" and self.sql_calls >= int(self.max_sql_calls):
            raise BudgetExceeded(
                f"Agent SQL-call budget exceeded ({self.max_sql_calls})"
            )
        if (
            action == "sql_query"
            and self.conversation_sql_calls >= int(self.max_conversation_sql_calls)
        ):
            raise BudgetExceeded(
                "Conversation SQL-call budget exceeded "
                f"({self.max_conversation_sql_calls})"
            )
        self.tool_calls += 1
        self.conversation_tool_calls += 1
        if action == "sql_query":
            self.sql_calls += 1
            self.conversation_sql_calls += 1

    def conversation_usage(self) -> Dict[str, int]:
        return {
            "llm_calls": self.conversation_llm_calls,
            "tool_calls": self.conversation_tool_calls,
            "sql_calls": self.conversation_sql_calls,
            "estimated_tokens": self.conversation_tokens,
        }
