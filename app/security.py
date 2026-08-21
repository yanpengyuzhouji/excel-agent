"""Fail-closed API-key authentication shared with the RAG gateway protocol."""

from __future__ import annotations

import hmac
import json
import os
import re
from dataclasses import dataclass
from typing import Mapping, Optional


_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
_MIN_KEY_CHARS = 24


class AuthConfigurationError(RuntimeError):
    pass


class AuthenticationError(ValueError):
    pass


@dataclass(frozen=True)
class ApiKeyAuthenticator:
    required: bool
    keys: tuple[tuple[str, str], ...]

    @classmethod
    def from_config(
        cls, config: Mapping[str, object], environ: Optional[Mapping[str, str]] = None
    ) -> "ApiKeyAuthenticator":
        env = environ if environ is not None else os.environ
        security = config.get("security", {})
        security = security if isinstance(security, Mapping) else {}
        required = bool(security.get("require_auth", True))
        json_env = str(security.get("api_keys_env", "RONGNENG_API_KEYS_JSON"))
        single_env = str(security.get("single_api_key_env", "RONGNENG_API_KEY"))
        principal_env = str(
            security.get("single_principal_env", "RONGNENG_API_PRINCIPAL")
        )

        parsed: dict[str, str] = {}
        raw_json = env.get(json_env, "").strip()
        if raw_json:
            try:
                value = json.loads(raw_json)
            except json.JSONDecodeError as exc:
                raise AuthConfigurationError(f"{json_env} must be valid JSON") from exc
            if not isinstance(value, dict):
                raise AuthConfigurationError(f"{json_env} must be a JSON object")
            parsed.update({str(name): str(key) for name, key in value.items()})

        single_key = env.get(single_env, "").strip()
        if single_key:
            parsed[env.get(principal_env, "default").strip() or "default"] = single_key

        records: list[tuple[str, str]] = []
        for principal, key in parsed.items():
            if not _PRINCIPAL_RE.fullmatch(principal):
                raise AuthConfigurationError(f"Invalid API principal: {principal!r}")
            if len(key) < _MIN_KEY_CHARS:
                raise AuthConfigurationError(
                    f"API key for {principal!r} must contain at least {_MIN_KEY_CHARS} characters"
                )
            records.append((principal, key))
        return cls(required=required, keys=tuple(sorted(records)))

    @property
    def configured(self) -> bool:
        return bool(self.keys) or not self.required

    def ensure_configured(self) -> None:
        if not self.configured:
            raise AuthConfigurationError(
                "Authentication is required but no API keys are configured"
            )

    def authenticate(self, authorization: str = "", x_api_key: str = "") -> str:
        self.ensure_configured()
        if not self.required:
            return "anonymous"
        candidate = ""
        if authorization:
            scheme, separator, token = authorization.partition(" ")
            if not separator or scheme.lower() != "bearer":
                raise AuthenticationError("Authorization must use the Bearer scheme")
            candidate = token.strip()
        elif x_api_key:
            candidate = x_api_key.strip()
        if not candidate:
            raise AuthenticationError("Missing API key")

        matched_principal: Optional[str] = None
        for principal, expected in self.keys:
            if hmac.compare_digest(candidate, expected):
                matched_principal = principal
        if matched_principal is None:
            raise AuthenticationError("Invalid API key")
        return matched_principal
