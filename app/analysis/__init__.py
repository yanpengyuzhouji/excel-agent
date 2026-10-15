"""Deterministic, model-independent workbook analysis primitives.

The modules in this package deliberately operate on the reviewed SQLite
workbook and produce JSON-shaped dictionaries.  They are the shared contract
between the large-model ReAct route and the small-model template route.
"""

from .profile import build_workbook_profile

__all__ = ["build_workbook_profile"]
