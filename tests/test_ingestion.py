import json
from pathlib import Path

import pytest

from database import Database


@pytest.fixture
def db():
    database = Database(":memory:")
    database.init()
    return database


def test_ingest_jira_raw_reference(db):
    # Load reference JSON
    raw_path = Path(__file__).parent / "raw_jira_issues.json"
    with raw_path.open("r", encoding="utf-8") as f:
        issues = json.load(f)

    # Ingest
    count = db.parse_and_ingest_jira_search_result(issues, sprint_name="Sprint 42")
    assert count > 0

    # Verify raw events in db
    cursor = db.conn.execute("SELECT COUNT(*) FROM raw_jira_events")
    raw_count = cursor.fetchone()[0]
    assert raw_count == 4  # 4 changelog items we defined in json

    # Check some content
    cursor = db.conn.execute(
        "SELECT field, from_value, to_value, author, author_email FROM raw_jira_events WHERE issue_key = 'PROJ-653' AND field = 'status'"
    )
    row = cursor.fetchone()
    assert row is not None
    assert row[0] == "status"
    assert row[1] == "To Do"
    assert row[2] == "In Progress"
    assert row[3] == "User A"
    assert row[4] == "user.a"

    # Normalise and verify
    db.normalise()
    timeline = db.get_ticket_timeline("PROJ-653")
    assert len(timeline) > 0

    # Check that status change and points are extracted
    status_changes = [t for t in timeline if t["event_type"] == "status_change"]
    assert len(status_changes) == 3

    point_assignments = [t for t in timeline if t["event_type"] == "points_assigned"]
    assert len(point_assignments) == 1
    assert point_assignments[0]["points"] == 5.0


def test_ingest_gitlab_raw_reference(db):
    # Load reference JSON
    raw_path = Path(__file__).parent / "raw_gitlab_mrs.json"
    with raw_path.open("r", encoding="utf-8") as f:
        mrs = json.load(f)

    # Ingest
    count = db.parse_and_ingest_gitlab_mr_events(mrs, milestone="Sprint 42")
    assert count > 0

    # Verify raw events in db
    cursor = db.conn.execute("SELECT COUNT(*) FROM raw_gitlab_events")
    raw_count = cursor.fetchone()[0]
    assert raw_count == 4  # created + 2 state_change + merged

    # Normalise and verify
    db.normalise()
    # The MR title in raw_gitlab_mrs.json contains "PROJ-653", so it should be linked to that key
    timeline = db.get_ticket_timeline("PROJ-653")
    assert len(timeline) > 0

    # Check for mr_merged event
    merged_events = [t for t in timeline if t["event_type"] == "mr_merged"]
    assert len(merged_events) == 2
    assert (
        merged_events[0]["author"] == "User B" or merged_events[1]["author"] == "User B"
    )
    assert any(e["author_id"] == "user.b" for e in merged_events)
