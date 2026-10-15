"""Model-to-analysis-route selection.

Route selection is explicit and inspectable.  The system does not use a
fragile model-name allow-list as the only guard: configuration wins, then
DeepSeek is treated as the large-model path, then a parsed parameter size is
used with a 30B boundary.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional


def infer_model_size_b(config: Dict[str, Any]) -> Optional[float]:
    configured = config.get("model_size_b")
    if configured is not None:
        try:
            return float(configured)
        except (TypeError, ValueError):
            pass
    text = " ".join(str(config.get(key) or "") for key in ("name", "model", "label"))
    match = re.search(r"(?<![0-9])([0-9]+(?:\.[0-9]+)?)\s*[bB](?![a-zA-Z])", text)
    return float(match.group(1)) if match else None


def select_analysis_route(
    config: Optional[Dict[str, Any]],
    *,
    requested_route: Optional[str] = None,
) -> Dict[str, Any]:
    """Return ``small``/``large`` and the reason used to select it."""
    config = config or {}
    explicit = requested_route or config.get("analysis_route") or config.get("route")
    if explicit in {"small", "large"}:
        return {"route": explicit, "reason": "explicit", "model_size_b": infer_model_size_b(config)}
    text = " ".join(str(config.get(key) or "") for key in ("name", "model", "label")).casefold()
    if "deepseek" in text:
        return {"route": "large", "reason": "deepseek-default", "model_size_b": infer_model_size_b(config)}
    size = infer_model_size_b(config)
    if size is not None and size >= 30:
        return {"route": "large", "reason": "model-size-gte-30b", "model_size_b": size}
    return {"route": "small", "reason": "model-size-below-30b-or-unknown", "model_size_b": size}

