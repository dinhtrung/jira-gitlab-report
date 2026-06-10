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
        INSERT INTO raw_jira_events (issue_key, field, from_value, to_value, timestamp, author, author_email, sprint_name)
        VALUES ('PROJ-1', 'status', 'To Do', 'In Progress', '2025-01-01T10:00:00Z', 'User A', 'user.a@example.com', 'Sprint 1')
    """)
    db.conn.execute("""
        INSERT INTO raw_jira_events (issue_key, field, from_value, to_value, timestamp, author, author_email, sprint_name)
        VALUES ('PROJ-1', 'Story Points', NULL, '5', '2025-01-01T10:05:00Z', 'User A', 'user.a@example.com', 'Sprint 1')
    """)

    db.normalise()

    history = db.get_ticket_timeline('PROJ-1')
    assert len(history) == 2
    assert history[0]['event_type'] == 'status_change'
    assert history[0]['author_id'] == 'user.a@example.com'
    assert history[1]['event_type'] == 'points_assigned'
    assert history[1]['points'] == 5.0
    assert history[1]['author_id'] == 'user.a@example.com'

def test_database_normalisation_gitlab(db):
    # Insert raw gitlab event with Jira key in title
    db.conn.execute("""
        INSERT INTO raw_gitlab_events (mr_iid, project_id, action, from_state, to_state, timestamp, author, author_username, milestone, title)
        VALUES (123, 1, 'state_change', 'opened', 'merged', '2025-01-01T12:00:00Z', 'User B', 'user.b', 'M1', 'Resolve PROJ-653: Add Feature X')
    """)

    db.normalise()

    # Should be linked to PROJ-653 instead of !123
    history = db.get_ticket_timeline('PROJ-653')
    assert len(history) == 1
    assert history[0]['event_type'] == 'mr_merged'
    assert history[0]['author_id'] == 'user.b'

def test_jira_regression_review_to_in_progress(db):
    db.conn.execute("""
        INSERT INTO raw_jira_events (issue_key, field, from_value, to_value, timestamp, author, author_email)
        VALUES ('PROJ-1', 'status', 'In Review', 'In Progress', '2025-01-01T10:00:00Z', 'User A', 'user.a')
    """)
    db.normalise()
    history = db.get_ticket_timeline('PROJ-1')
    assert history[0]['event_type'] == 'returned_to_progress'
