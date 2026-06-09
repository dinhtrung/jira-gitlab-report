"""
config.py — Central configuration for jira-gitlab-report.

All values have sensible defaults but can be overridden via environment
variables. Import this module from database.py and reconciler.py instead
of defining constants inline.

Override via env::

    export JIRA_DONE_STATUSES="Done,Closed,Resolved,Merged"
    export GATE_TRANSITIONS="code review approved,approved,merged"
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Jira status vocabulary
# ---------------------------------------------------------------------------

def _csv_set(var: str, default: set[str]) -> set[str]:
    """Parse env var as comma-separated set; fall back to *default*."""
    raw = os.getenv(var)
    if raw:
        return {s.strip() for s in raw.split(",") if s.strip()}
    return default


JIRA_DONE_STATUSES = _csv_set(
    "JIRA_DONE_STATUSES",
    {"Done", "Closed", "Resolved", "Complete", "Merged"},
)

REOPEN_WORDS = _csv_set(
    "REOPEN_WORDS",
    {"Reopened", "Reopen", "Return to Progress", "Back to In Progress"},
)

GATE_TRANSITIONS = _csv_set(
    "GATE_TRANSITIONS",
    {
        "code review approved",
        "review approved",
        "ready for merge",
        "ready to merge",
        "approved",
        "merged",
        "mr merged",
    },
)

POINTS_FIELD_NAMES = _csv_set(
    "POINTS_FIELD_NAMES",
    {"story points", "story point estimate", "estimate", "effort"},
)
