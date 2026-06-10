"""
database.py — SQLite persistence layer for the Sprint Reporting Engine.

Provides:
  - Schema bootstrapping from schema.sql
  - Insert helpers for raw Jira / GitLab events
  - Normalization into ticket_history
  - Query methods to feed the reconciler
"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
from pathlib import Path
from typing import Any

from config import (
    GATE_TRANSITIONS,
    JIRA_DONE_STATUSES,
    POINTS_FIELD_NAMES,
    REOPEN_WORDS,
)

logger = logging.getLogger(__name__)

# Path to schema.sql relative to this file (project root)
_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


class Database:
    """Wraps a single SQLite connection with convenience helpers."""

    def __init__(self, db_path: str | Path = "sprint_data.db") -> None:
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialised — call init() first")
        return self._conn

    def init(self) -> None:
        """Create / migrate schema."""
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        self._conn.executescript(schema_sql)
        self._conn.commit()
        logger.info("Database ready at %s", self.db_path)

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # -- raw Jira ingestion ------------------------------------------------

    def insert_raw_jira_event(
        self,
        issue_key: str,
        field: str,
        from_value: str | None,
        to_value: str | None,
        timestamp: str,
        author: str = "",
        author_email: str = "",
        sprint_name: str = "",
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO raw_jira_events (issue_key, field, from_value, to_value,
               timestamp, author, author_email, sprint_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (issue_key, field, from_value, to_value, timestamp, author, author_email, sprint_name),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def parse_and_ingest_jira_changelog(
        self, issue_key: str, changelog_items: list[dict], sprint_name: str = ""
    ) -> int:
        """Consume a Jira changelog JSON object and persist every history entry."""
        count = 0
        for entry in changelog_items:
            created = entry.get("created", "")
            author_obj = entry.get("author") or {}
            author = author_obj.get("displayName") or author_obj.get("display_name", "")
            author_email = (
                author_obj.get("emailAddress")
                or author_obj.get("email")
                or author_obj.get("name")
                or ""
            )
            for item in entry.get("items", []):
                from_value = (
                    item.get("fromString") if "fromString" in item else item.get("from_string")
                )
                to_value = item.get("toString") if "toString" in item else item.get("to_string")
                self.insert_raw_jira_event(
                    issue_key=issue_key,
                    field=item.get("field", ""),
                    from_value=from_value,
                    to_value=to_value,
                    timestamp=created,
                    author=author,
                    author_email=author_email,
                    sprint_name=sprint_name,
                )
                count += 1
        return count

    def parse_and_ingest_jira_search_result(self, issues: list[dict], sprint_name: str = "") -> int:
        """Process a Jira search result with embedded changelog data.

        Each issue dict is expected to carry ``changelog`` under its
        ``_changelog`` or ``changelog`` key after an ``expand=changelog`` request,
        or as a top-level list of ``changelogs``.
        """
        count = 0
        for issue in issues:
            key = issue.get("key", "")
            fields = issue.get("fields", {})
            if "changelogs" in issue:
                histories = issue["changelogs"]
            else:
                changelog = (
                    issue.get("changelog", {})
                    or issue.get("_changelog", {})
                    or fields.get("changelog", {})
                )
                histories = changelog.get("histories", []) if isinstance(changelog, dict) else []
            count += self.parse_and_ingest_jira_changelog(key, histories, sprint_name=sprint_name)
        return count

    # -- raw GitLab ingestion ----------------------------------------------

    def insert_raw_gitlab_event(
        self,
        mr_iid: int,
        project_id: int,
        action: str,
        from_state: str | None,
        to_state: str | None,
        timestamp: str,
        author: str = "",
        author_username: str = "",
        milestone: str = "",
        title: str = "",
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO raw_gitlab_events (mr_iid, project_id, action, from_state,
               to_state, timestamp, author, author_username, milestone, title)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                mr_iid,
                project_id,
                action,
                from_state,
                to_state,
                timestamp,
                author,
                author_username,
                milestone,
                title,
            ),
        )
        self.conn.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    def parse_and_ingest_gitlab_mr_events(
        self, mrs: list[dict], project_id: int = 0, milestone: str = ""
    ) -> int:
        """Consume a list of GitLab MR dicts (with resource_state_events if embedded).

        Each MR dict may carry a ``resource_state_events`` key with timeline entries.
        """
        count = 0
        for mr in mrs:
            mr_iid = mr.get("iid", 0)
            pid = mr.get("project_id", project_id)
            title = mr.get("title", "")
            self.insert_raw_gitlab_event(
                mr_iid=mr_iid,
                project_id=pid,
                action="created",
                from_state=None,
                to_state=mr.get("state", "opened"),
                timestamp=mr.get("created_at", ""),
                author=(mr.get("author") or {}).get("name", ""),
                author_username=(mr.get("author") or {}).get("username", ""),
                milestone=milestone or (mr.get("milestone") or {}).get("title", ""),
                title=title,
            )
            count += 1
            for ev in mr.get("resource_state_events", []):
                self.insert_raw_gitlab_event(
                    mr_iid=mr_iid,
                    project_id=pid,
                    action="state_change",
                    from_state=ev.get("from_state"),
                    to_state=ev.get("to_state"),
                    timestamp=ev.get("created_at", ""),
                    author=(ev.get("user") or {}).get("name", ""),
                    author_username=(ev.get("user") or {}).get("username", ""),
                    milestone=milestone,
                    title=title,
                )
                count += 1
            if mr.get("merged_at"):
                self.insert_raw_gitlab_event(
                    mr_iid=mr_iid,
                    project_id=pid,
                    action="merged",
                    from_state="merged",
                    to_state="merged",
                    timestamp=mr["merged_at"],
                    author=(mr.get("merged_by") or {}).get("name", ""),
                    author_username=(mr.get("merged_by") or {}).get("username", ""),
                    milestone=milestone,
                    title=title,
                )
                count += 1
        return count

    # -- normalisation → ticket_history ------------------------------------

    def normalise(self) -> int:
        """Convert raw_jira_events and raw_gitlab_events into ticket_history.

        Returns the number of normalised rows inserted.
        """
        count = 0
        self.conn.execute("DELETE FROM ticket_history")
        # --- Jira ---
        rows = self.conn.execute(
            "SELECT * FROM raw_jira_events ORDER BY issue_key, timestamp"
        ).fetchall()
        for r in rows:
            etype, old_s, new_s = _classify_jira_event(r["field"], r["from_value"], r["to_value"])
            pts = 0.0
            if etype == "points_assigned" and new_s:
                with contextlib.suppress(ValueError, TypeError):
                    pts = float(new_s)
            self.conn.execute(
                """INSERT INTO ticket_history
                   (ticket_id, source, event_type, old_status, new_status,
                    points, sprint_name, author, author_id, event_ts)
                   VALUES (?, 'jira', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    r["issue_key"],
                    etype,
                    old_s,
                    new_s,
                    pts,
                    r["sprint_name"],
                    r["author"],
                    r["author_email"],
                    r["timestamp"],
                ),
            )
            count += 1
        # --- GitLab ---
        rows = self.conn.execute(
            "SELECT * FROM raw_gitlab_events ORDER BY mr_iid, timestamp"
        ).fetchall()
        for r in rows:
            etype, old_s, new_s = _classify_gitlab_event(
                r["action"], r["from_state"], r["to_state"]
            )

            # Link to Jira ticket if found in title
            ticket_id = f"!{r['mr_iid']}"
            if r["title"]:
                # Matches patterns like OCTD-123 (uppercase letters + hyphen + numbers)
                match = re.search(r"([A-Z]+-\d+)", r["title"])
                if match:
                    ticket_id = match.group(1)

            self.conn.execute(
                """INSERT INTO ticket_history
                   (ticket_id, source, event_type, old_status, new_status,
                    points, sprint_name, author, author_id, event_ts)
                   VALUES (?, 'gitlab', ?, ?, ?, 0, ?, ?, ?, ?)""",
                (
                    ticket_id,
                    etype,
                    old_s,
                    new_s,
                    r["milestone"],
                    r["author"],
                    r["author_username"],
                    r["timestamp"],
                ),
            )
            count += 1
        self.conn.commit()
        logger.info("Normalised %d history rows", count)
        return count

    # -- queries for the reconciler ----------------------------------------

    def get_tickets_in_range(self, start_date: str, end_date: str) -> list[dict]:
        """Return distinct ticket_ids with events touching [start_date, end_date]."""
        rows = self.conn.execute(
            """SELECT DISTINCT ticket_id, source, sprint_name
               FROM ticket_history
               WHERE event_ts BETWEEN ? AND ?
               ORDER BY ticket_id""",
            (start_date, end_date),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_ticket_timeline(self, ticket_id: str) -> list[dict]:
        """Return every event for *ticket_id* ordered by timestamp."""
        rows = self.conn.execute(
            "SELECT * FROM ticket_history WHERE ticket_id = ? ORDER BY event_ts",
            (ticket_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def upsert_report_row(
        self,
        ticket_id: str,
        source: str,
        final_status: str,
        awarded_points: float,
        flag: str,
        sprint_name: str,
        start_date: str,
        end_date: str,
    ) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO sprint_report
               (sprint_name, start_date, end_date, ticket_id, source,
                final_status, awarded_points, flag)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sprint_name,
                start_date,
                end_date,
                ticket_id,
                source,
                final_status,
                awarded_points,
                flag,
            ),
        )
        self.conn.commit()

    def get_report(
        self, sprint_name: str = "", start_date: str = "", end_date: str = ""
    ) -> list[dict]:
        """Read back previously computed sprint report rows."""
        query = "SELECT * FROM sprint_report WHERE 1=1"
        params: list[Any] = []
        if sprint_name:
            query += " AND sprint_name = ?"
            params.append(sprint_name)
        if start_date:
            query += " AND start_date = ?"
            params.append(start_date)
        if end_date:
            query += " AND end_date = ?"
            params.append(end_date)
        query += " ORDER BY ticket_id"
        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


# -----------------------------------------------------------------------
# Internal classification helpers
# -----------------------------------------------------------------------


def _classify_jira_event(
    field: str, from_val: str | None, to_val: str | None
) -> tuple[str, str | None, str | None]:
    """Map a Jira changelog field+value pair to a ticket_history event_type."""
    field_l = field.lower()
    to_s = str(to_val) if to_val else None
    from_s = str(from_val) if from_val else None
    to_l = str(to_val or "").lower()

    if field_l == "status":
        if any(gate in to_l for gate in GATE_TRANSITIONS):
            return ("code_review_approved", from_s, to_s)
        if to_val and any(w.lower() in to_l for w in REOPEN_WORDS):
            return ("reopened", from_s, to_s)
        # Check for regression from Review to In Progress
        if from_val and to_val:
            from_l = str(from_val).lower()
            if "review" in from_l and "progress" in to_l:
                return ("returned_to_progress", from_s, to_s)
        return ("status_change", from_s, to_s)
    if field_l in ("resolution",) and to_s in JIRA_DONE_STATUSES:
        return ("status_change", from_s, to_s)
    if field_l == "sprint":
        return ("status_change", from_s, to_s)
    if field_l == "issuetype":
        return ("created", None, to_s)
    # Story-points field changes
    if any(pat in field_l for pat in POINTS_FIELD_NAMES) and to_val is not None:
        return ("points_assigned", from_s, to_s)
    return ("status_change", from_s, to_s)


def _classify_gitlab_event(
    action: str, from_state: str | None, to_state: str | None
) -> tuple[str, str | None, str | None]:
    """Map a GitLab resource state event to a ticket_history event_type."""
    if action == "created":
        return ("created", None, str(to_state))
    if action == "merged":
        return ("mr_merged", None, "merged")
    if action == "state_change":
        to_l = str(to_state or "").lower()
        from_l = str(from_state or "").lower()
        if to_l == "merged":
            return ("mr_merged", from_l, to_l)
        if "reopen" in to_l or (from_l in ("closed", "merged") and to_l == "opened"):
            return ("reopened", from_l, to_l)
        return ("status_change", from_l, to_l)
    return ("status_change", str(from_state), str(to_state))
