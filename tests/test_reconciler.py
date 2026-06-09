import pytest
from database import Database
from reconciler import Reconciler

@pytest.fixture
def db():
    database = Database(":memory:")
    database.init()
    return database

def test_reconciler_points_logic(db):
    # Setup timeline with multiple point assignments
    db.conn.execute("""
        INSERT INTO ticket_history (ticket_id, source, event_type, points, event_ts, new_status)
        VALUES ('PROJ-1', 'jira', 'points_assigned', 3, '2025-01-01T10:00:00Z', 'In Progress')
    """)
    db.conn.execute("""
        INSERT INTO ticket_history (ticket_id, source, event_type, points, event_ts, new_status)
        VALUES ('PROJ-1', 'jira', 'points_assigned', 5, '2025-01-01T11:00:00Z', 'In Progress')
    """)
    db.conn.execute("""
        INSERT INTO ticket_history (ticket_id, source, event_type, event_ts, new_status)
        VALUES ('PROJ-1', 'jira', 'mr_merged', '2025-01-01T12:00:00Z', 'Done')
    """)

    reconciler = Reconciler(db)
    report = reconciler.reconcile('2025-01-01', '2025-01-02')

    assert len(report.tickets) == 1
    assert report.tickets[0].points == 5.0
    assert report.tickets[0].flag == 'valid'

def test_reconciler_regression_logic(db):
    # Setup timeline with regression after gate
    db.conn.execute("""
        INSERT INTO ticket_history (ticket_id, source, event_type, points, event_ts, new_status)
        VALUES ('PROJ-2', 'jira', 'points_assigned', 3, '2025-01-01T10:00:00Z', 'In Progress')
    """)
    db.conn.execute("""
        INSERT INTO ticket_history (ticket_id, source, event_type, event_ts, new_status)
        VALUES ('PROJ-2', 'jira', 'code_review_approved', '2025-01-01T11:00:00Z', 'In Progress')
    """)
    db.conn.execute("""
        INSERT INTO ticket_history (ticket_id, source, event_type, event_ts, new_status)
        VALUES ('PROJ-2', 'jira', 'reopened', '2025-01-01T12:00:00Z', 'Reopened')
    """)

    reconciler = Reconciler(db)
    report = reconciler.reconcile('2025-01-01', '2025-01-02')

    assert report.tickets[0].flag == 'regressed'
    assert report.tickets[0].points == 0.0
    assert report.tickets[0].regression_count == 1
