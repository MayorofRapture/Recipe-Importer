"""Small user-facing status contracts shared by adapters and Streamlit pages."""
from __future__ import annotations


def normalize_status(
    raw_status: str,
    *,
    candidate_complete: bool,
    requires_review: bool,
    has_pending_suggestions: bool,
    has_critical_findings: bool,
    approved: bool = False,
) -> str:
    raw = (raw_status or "").upper()
    if raw == "IMPORTED" or raw == "TAG_FAILED":
        return "Imported"
    if raw in {"UNCERTAIN", "NEEDS_MANUAL_CHECK"}:
        return "Check Mealie"
    if raw in {"IMPORT_FAILED", "EXTRACTION_FAILED", "WORKER_FAILED"}:
        return "Import failed" if raw == "IMPORT_FAILED" else "Needs attention"
    if not candidate_complete or requires_review or has_pending_suggestions or has_critical_findings:
        return "Needs attention"
    if approved:
        return "Ready to import"
    return "Ready to review"
