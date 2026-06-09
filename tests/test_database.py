import pytest
from database import Database

@pytest.fixture
def db():
    database = Database(":memory:")
    database.init()
    return database

def test_database_normalisation_jira(db):
    # Insert raw jira event
    db.conn.execute("""
        INSERT INTO raw_jira_events (issue_key, field, from_value, to_value, timestamp, author, sprint_name)
        VALUES ('PROJ-1', 'status', 'To Do', 'In Progress', '2025-01-01T10:00:00Z', 'User A', 'Sprint 1')
    """)
    db.conn.execute("""
        INSERT INTO raw_jira_events (issue_key, field, from_value, to_value, timestamp, author, sprint_name)
        VALUES ('PROJ-1', 'Story Points', NULL, '5', '2025-01-01T10:05:00Z', 'User A', 'Sprint 1')
    """)

    db.normalise()

    history = db.get_ticket_timeline('PROJ-1')
    assert len(history) == 2
    assert history[0]['event_type'] == 'status_change'
    assert history[1]['event_type'] == 'points_assigned'
    assert history[1]['points'] == 5.0

def test_database_normalisation_gitlab(db):
    # Insert raw gitlab event
    db.conn.execute("""
        INSERT INTO raw_gitlab_events (mr_iid, project_id, action, from_state, to_state, timestamp, author, milestone)
        VALUES (123, 1, 'state_change', 'opened', 'merged', '2025-01-01T12:00:00Z', 'User B', 'M1')
    """)

    db.normalise()

    history = db.get_ticket_timeline('!123')
    assert len(history) == 1
    assert history[0]['event_type'] == 'mr_merged'
