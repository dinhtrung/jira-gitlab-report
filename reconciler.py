"""
reconciler.py — Deterministic state-machine engine for sprint velocity.

**Core Rules**

1. *Linearization* — For every ticket ID within the date range, reconstruct
   its full timeline from ticket_history ordered by event_ts.

2. *Point Attribution* — A ticket earns its story-points **only if**:
   (a) Its final status at end_date is in the DONE set.
   (b) There are **zero** ``reopen`` or ``return_to_progress`` events
       **after** the most recent ``code_review_approved`` / ``mr_merged``
       event (i.e., no regression after the last successful gate).

3. *Invalidation* — If a ticket was worked on (has events) but its end-
   of-range status is NOT a done status, it is flagged **In‑Flight** and
   assigned 0 points, regardless of prior effort.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from config import JIRA_DONE_STATUSES
from database import Database

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Status-words that signal a ticket was actively regressed.
# ---------------------------------------------------------------------------
REGRESSION_EVENTS = {"reopened", "returned_to_progress"}

# Events that mark a "gate" after which no regression is allowed.
GATE_EVENTS = {"code_review_approved", "mr_merged"}

# GitLab "merged" status is equivalent to Done.
GITLAB_DONE_STATUSES = {"merged"}

ALL_DONE_STATUSES = JIRA_DONE_STATUSES | GITLAB_DONE_STATUSES


# ---------------------------------------------------------------------------
# Output structures
# ---------------------------------------------------------------------------


@dataclass
class TicketVerdict:
    """The reconciler's decision for one ticket."""

    ticket_id: str
    source: str
    final_status: str
    points: float
    flag: str  # 'valid', 'in-flight', 'regressed', 'no-gate', 'not-done', 'untouched'
    regression_count: int = 0
    cycle_time_hours: dict[str, float] = field(default_factory=dict)
    detail: str = ""


@dataclass
class SprintReport:
    """Aggregated report result."""

    sprint_name: str
    start_date: str
    end_date: str
    tickets: list[TicketVerdict] = field(default_factory=list)

    @property
    def total_points(self) -> float:
        return sum(t.points for t in self.tickets if t.flag in ("valid",))

    @property
    def in_flight_count(self) -> int:
        return sum(1 for t in self.tickets if t.flag == "in-flight")

    @property
    def regressed_count(self) -> int:
        return sum(1 for t in self.tickets if t.flag == "regressed")

    @property
    def total_regressions(self) -> int:
        return sum(t.regression_count for t in self.tickets)

    @property
    def valid_count(self) -> int:
        return sum(1 for t in self.tickets if t.flag == "valid")


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------


class Reconciler:
    """Applies deterministic rules to a database of ticket events."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def reconcile(self, start_date: str, end_date: str) -> SprintReport:
        """Run the full reconciliation for a date range.

        Returns a SprintReport suitable for Markdown rendering.
        """
        tickets = self.db.get_tickets_in_range(start_date, end_date)
        logger.info("Reconciling %d tickets for [%s … %s]", len(tickets), start_date, end_date)

        verdicts: list[TicketVerdict] = []
        for tkt in tickets:
            ticket_id = tkt["ticket_id"]
            source = tkt["source"]
            sprint_name = tkt.get("sprint_name", "") or ""

            timeline = self.db.get_ticket_timeline(ticket_id)
            verdict = self._judge(ticket_id, source, timeline, start_date, end_date)

            self.db.upsert_report_row(
                ticket_id=ticket_id,
                source=source,
                final_status=verdict.final_status,
                awarded_points=verdict.points,
                flag=verdict.flag,
                sprint_name=sprint_name,
                start_date=start_date,
                end_date=end_date,
            )
            verdicts.append(verdict)

        sprint_name = next((t["sprint_name"] for t in tickets if t.get("sprint_name")), "")

        return SprintReport(
            sprint_name=sprint_name,
            start_date=start_date,
            end_date=end_date,
            tickets=verdicts,
        )

    # ------------------------------------------------------------------
    # Core state machine
    # ------------------------------------------------------------------

    def _judge(
        self,
        ticket_id: str,
        source: str,
        timeline: list[dict],
        start_date: str,
        end_date: str,
    ) -> TicketVerdict:
        """Apply the deterministic rules to one ticket's timeline."""
        range_events = [e for e in timeline if start_date <= e["event_ts"] <= end_date]
        if not range_events:
            return TicketVerdict(
                ticket_id=ticket_id,
                source=source,
                final_status="unknown",
                points=0.0,
                flag="untouched",
                detail="No events in the reporting period.",
            )

        final_status = self._resolve_status_at(timeline, end_date)
        points = self._extract_points(timeline)
        cycle = self._calculate_cycle_time(timeline, start_date, end_date)

        # Count ALL regression events in range (total insight, not just post-gate)
        total_regressions = sum(1 for e in range_events if e["event_type"] in REGRESSION_EVENTS)

        last_gate = self._find_last_gate(range_events)

        if last_gate is None:
            done = final_status.lower() in (s.lower() for s in ALL_DONE_STATUSES)
            if done:
                return TicketVerdict(
                    ticket_id=ticket_id,
                    source=source,
                    final_status=final_status,
                    points=0.0,
                    flag="no-gate",
                    regression_count=total_regressions,
                    cycle_time_hours=cycle,
                    detail="Done but no code-review-approved or mr-merged event found in period.",
                )
            return TicketVerdict(
                ticket_id=ticket_id,
                source=source,
                final_status=final_status,
                points=0.0,
                flag="in-flight",
                regression_count=total_regressions,
                cycle_time_hours=cycle,
                detail=f"Worked on (final: '{final_status}') but no review/merge gate reached.",
            )

        regression_after_gate = [
            e
            for e in range_events
            if e["event_type"] in REGRESSION_EVENTS and e["event_ts"] >= last_gate["event_ts"]
        ]
        if regression_after_gate:
            return TicketVerdict(
                ticket_id=ticket_id,
                source=source,
                final_status=final_status,
                points=0.0,
                flag="regressed",
                regression_count=total_regressions,
                cycle_time_hours=cycle,
                detail=f"{len(regression_after_gate)} regression(s) after last gate ({total_regressions} total).",
            )

        if final_status.lower() in (s.lower() for s in ALL_DONE_STATUSES):
            return TicketVerdict(
                ticket_id=ticket_id,
                source=source,
                final_status=final_status,
                points=points,
                flag="valid",
                regression_count=total_regressions,
                cycle_time_hours=cycle,
                detail=f"Gate passed: {last_gate['event_type']} at {last_gate['event_ts']}.",
            )

        return TicketVerdict(
            ticket_id=ticket_id,
            source=source,
            final_status=final_status,
            points=0.0,
            flag="in-flight",
            regression_count=total_regressions,
            cycle_time_hours=cycle,
            detail=f"Worked on but final status is '{final_status}' — not a done state.",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_status_at(timeline: list[dict], cutoff: str) -> str:
        current = "unknown"
        for e in timeline:
            if e["event_ts"] > cutoff:
                break
            if e["new_status"]:
                current = e["new_status"]
        return current

    @staticmethod
    def _calculate_cycle_time(
        timeline: list[dict], start_date: str, end_date: str
    ) -> dict[str, float]:
        """Compute hours spent in each status within the date range."""
        from datetime import datetime

        status_duration: dict[str, float] = {}
        last_ts: str | None = None
        last_status: str | None = None

        for e in timeline:
            ts = e["event_ts"]
            new_s = e.get("new_status")
            if new_s is None:
                continue

            range_start = max(ts, start_date)

            if last_ts is not None and last_status is not None:
                range_end = min(ts, end_date)
                try:
                    t0 = datetime.fromisoformat(last_ts)
                    t1 = datetime.fromisoformat(range_end)
                    delta_h = (t1 - t0).total_seconds() / 3600.0
                    if delta_h > 0:
                        status_duration[last_status] = (
                            status_duration.get(last_status, 0.0) + delta_h
                        )
                except (ValueError, TypeError):
                    pass

            last_ts = ts
            last_status = new_s

        # Handle the last known status → end_date
        if last_ts is not None and last_status is not None:
            try:
                t0 = datetime.fromisoformat(last_ts)
                t1 = datetime.fromisoformat(end_date)
                delta_h = (t1 - t0).total_seconds() / 3600.0
                if delta_h > 0:
                    status_duration[last_status] = (
                        status_duration.get(last_status, 0.0) + delta_h
                    )
            except (ValueError, TypeError):
                pass

        return status_duration

    @staticmethod
    def _extract_points(timeline: list[dict]) -> float:
        """Return the most recent story-point value (last assigned wins)."""
        pts = 0.0
        for e in timeline:
            if e["event_type"] == "points_assigned":
                pts = e.get("points", 0.0)
        return pts

    @staticmethod
    def _find_last_gate(events: list[dict]) -> dict | None:
        last: dict | None = None
        for e in events:
            if e["event_type"] in GATE_EVENTS and (
                last is None or e["event_ts"] > last["event_ts"]
            ):
                last = e
        return last
