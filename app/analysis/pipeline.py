"""Orchestration for the deterministic generic analysis bundle."""

from __future__ import annotations

from typing import Any, Dict

from .findings import derive_findings
from .profile import build_workbook_profile
from .report import build_report_spec
from .templates import run_standard_templates


def build_analysis_bundle(service: Any, workbook_id: str) -> Dict[str, Any]:
    """Build profile, facts, findings and a validated report spec."""
    profile = build_workbook_profile(service, workbook_id)
    facts = run_standard_templates(service, workbook_id, profile)
    findings = derive_findings(profile, facts)
    report = build_report_spec(profile, facts, findings)
    return {
        "profile": profile,
        "facts": facts,
        "findings": findings,
        "report": report,
    }

