"""OpenAI-compatible LLM client for the Excel service (phase 2).

Uses the openai SDK pointed at an arbitrary OpenAI-compatible /v1 endpoint
(Excel-specific config block `excel.llm`, independent from RongNengRAG's llm).
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)


def _read_env_file(path: Optional[str]) -> Dict[str, str]:
    """Parse a simple ``KEY=value`` / ``export KEY=value`` env file into a dict.

    Used as a fallback when an api key is not present in the OS environment:
    lets ``excel.llm.env_file`` point at a shared ``.env`` (e.g. DB-GPT's
    ``.env.deepseek``) without hard-coding secrets into config.yaml.
    """
    if not path:
        return {}
    try:
        import os as _os
        from pathlib import Path as _Path

        # Resolve a relative path against the config directory (project root),
        # so ``env_file: ".env"`` works regardless of the process cwd.
        if not _os.path.isabs(path):
            _cfg = _Path(__file__).resolve().parent.parent.parent / "config.yaml"
            path = str(_Path(_cfg).parent / path)
        if not _os.path.exists(path):
            return {}
        result: Dict[str, str] = {}
        with open(path, "r", encoding="utf-8") as stream:
            for raw in stream:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :]
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key:
                    result[key] = value
        return result
    except OSError:
        return {}


class ExcelLLM:
    """Thin OpenAI-compatible chat client with stream and non-stream methods."""

    def __init__(self, llm_config: Dict[str, Any]):
        import openai

        self._config = llm_config or {}
        base_url = str(self._config.get("base_url") or "").strip()
        base_url_env = str(self._config.get("base_url_env") or "").strip()
        if base_url_env:
            # Keep provider endpoints out of config.yaml when the deployment
            # already supplies them through .env, just like API keys.
            env_base_url = os.environ.get(base_url_env, "").strip()
            if not env_base_url:
                env_base_url = _read_env_file(self._config.get("env_file")).get(
                    base_url_env, ""
                ).strip()
            base_url = env_base_url or base_url
        base_url = base_url or "http://127.0.0.1:18000/v1"
        model = self._config.get("model") or "Qwen3-4B"
        api_key = self._config.get("api_key") or ""
        env_key = self._config.get("api_key_env")
        if env_key:
            api_key = os.environ.get(env_key, api_key)
        if not api_key and env_key:
            # Fallback to a shared env file (e.g. DB-GPT's .env.deepseek) so the
            # secret never has to live in config.yaml.
            env_file = self._config.get("env_file")
            api_key = _read_env_file(env_file).get(env_key, "")
        if not api_key:
            api_key = "sk-empty"
        self.model = model
        self.temperature = float(self._config.get("temperature", 0.1))
        self.max_tokens = int(self._config.get("max_tokens", 4096))
        # A blocked provider must not make the synchronous ReAct generator
        # look dead for several minutes.  Keep the values configurable, but
        # default to fail-fast semantics; callers can opt into retries for a
        # provider that is known to benefit from them.
        self.timeout = float(self._config.get("timeout", 90))
        self.first_token_timeout = float(self._config.get("first_token_timeout", 10))
        self.max_retries = max(0, int(self._config.get("max_retries", 0)))
        self.use_stream = bool(self._config.get("stream", False))
        self.last_finish_reason = None
        self.deadline = None
        # vLLM-specific chat-template options (for example Qwen3's
        # ``enable_thinking`` switch).  Keep this optional so DeepSeek and
        # other OpenAI-compatible endpoints receive the same request shape as
        # before.
        extra_body = self._config.get("extra_body")
        self.extra_body = extra_body if isinstance(extra_body, dict) else None
        self._client = openai.OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=self.timeout,
            max_retries=self.max_retries,
        )
        if str(model).strip().casefold() in {"auto", "__auto__"}:
            model = self._discover_model_id()
        self.model = model

    def _discover_model_id(self) -> str:
        """Resolve a single served model from an OpenAI-compatible endpoint.

        vLLM's model id is deployment-specific: it may be the local filesystem
        path, a short served name, or a custom alias.  Allowing ``model: auto``
        in configuration keeps that detail at the endpoint where it belongs,
        while still sending the exact id required by ``/v1/chat/completions``.
        """
        try:
            response = self._client.models.list()
            items = getattr(response, "data", None) or []
            model_ids = [str(item.id).strip() for item in items if getattr(item, "id", None)]
        except Exception as exc:
            raise RuntimeError(
                "Unable to discover the served model from the configured "
                f"vLLM endpoint: {type(exc).__name__}: {exc}"
            ) from exc
        if not model_ids:
            raise RuntimeError(
                "The configured vLLM endpoint returned no models from /v1/models"
            )
        if len(set(model_ids)) > 1:
            raise RuntimeError(
                "The configured vLLM endpoint returned multiple models; set "
                f"an explicit model id instead of auto: {', '.join(model_ids)}"
            )
        return model_ids[0]

    @property
    def model_name(self) -> str:
        return self.model

    def _bounded_request(self, request):
        """Bound the whole SDK call, including streams that only send heartbeats."""
        results = queue.Queue(maxsize=8)
        stopped = threading.Event()
        end = time.monotonic() + self.timeout
        first_token_end = time.monotonic() + self.first_token_timeout
        received_content = False
        if isinstance(self.deadline, (int, float)):
            end = min(end, self.deadline)

        def put(value):
            while not stopped.is_set():
                try:
                    results.put(value, timeout=.05)
                    return
                except queue.Full:
                    pass

        def receive():
            response = None
            try:
                response = self._client.chat.completions.create(**request)
                if request['stream']:
                    for chunk in response:
                        if stopped.is_set():
                            break
                        put(('value', chunk))
                else:
                    put(('value', response))
            except Exception as exc:
                put(('error', exc))
            finally:
                if request['stream'] and response is not None:
                    close = getattr(response, 'close', None)
                    if callable(close):
                        close()
                put(('done', None))

        threading.Thread(target=receive, daemon=True).start()
        completed = False
        try:
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f'模型 {self.model} 响应超时，请稍后重试或切换模型。')
                if request['stream'] and not received_content:
                    first_remaining = first_token_end - time.monotonic()
                    if first_remaining <= 0:
                        raise TimeoutError(f'模型 {self.model} 在 {self.first_token_timeout:g} 秒内未返回正文，请重试或切换模型。')
                    remaining = min(remaining, first_remaining)
                try:
                    kind, value = results.get(timeout=min(.1, remaining))
                except queue.Empty:
                    continue
                if kind == 'error':
                    raise value
                if kind == 'done':
                    completed = True
                    return
                if request['stream']:
                    choices = getattr(value, 'choices', None) or []
                    if choices and getattr(getattr(choices[0], 'delta', None), 'content', None):
                        received_content = True
                yield value
        finally:
            stopped.set()
            if not completed:
                # Closing a blocked transport must not delay releasing the run slot.
                threading.Thread(target=self._client.close, daemon=True).start()

    def generate_chat(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        self.last_finish_reason = None
        # Internally stream even buffered replies so the first-token deadline
        # measures actual content rather than completion of the entire answer.
        return "".join(self.generate_chat_stream(
            messages, temperature=temperature, max_tokens=max_tokens,
        ))

    def generate_chat_stream(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Generator[str, None, None]:
        self.last_finish_reason = None
        request: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": True,
        }
        if self.extra_body:
            request["extra_body"] = self.extra_body
        stream = self._bounded_request(request)
        emitted = False
        for chunk in stream:
            if chunk.choices and getattr(chunk.choices[0], 'finish_reason', None):
                self.last_finish_reason = chunk.choices[0].finish_reason
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                emitted = True
                yield chunk.choices[0].delta.content
        logger.info('llm completion model=%s finish_reason=%s', self.model, self.last_finish_reason)
        if not emitted:
            raise RuntimeError(
                f"LLM model {self.model!r} returned no visible streamed content; "
                "the provider may not have a healthy inference route for this model"
            )
