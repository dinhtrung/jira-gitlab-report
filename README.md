# jira-gitlab-report

Deterministic sprint performance reporting engine. Pulls changelog and MR
event data from Jira and GitLab MCP servers, reconciles timelines via a
state machine, and outputs a Markdown table with sprint velocity metrics.

## Quick start

```bash
# Install dependencies
pip install httpx

# Run a report
python main.py --start-date 2025-04-01 --end-date 2025-04-14 \
    --jira-url http://localhost:8080 \
    --gitlab-url http://localhost:8081
```

The Jira and GitLab URLs are MCP SSE endpoints exposed by local Docker
containers. See [MCP Streamable HTTP transport](https://spec.modelcontextprotocol.io/specification/2025-03-26/basic/transports/#streamable-http)
for details.

## Architecture

```
jira-gitlab-report/
├── main.py            # CLI: arg parsing, orchestration, Markdown rendering
├── sse_client.py      # Async MCP SSE client (httpx)
├── database.py        # SQLite schema, CRUD, raw → normalized pipeline
├── reconciler.py      # Deterministic state machine
├── schema.sql         # DDL (4 tables)
```

### Pipeline

```
MCP servers ──(SSE)──▶ raw_jira_events / raw_gitlab_events
                              │
                       normalise()
                              ▼
                      ticket_history
                              │
                       reconcile()
                              ▼
                       sprint_report ──▶ Markdown table
```

## CLI options

| Flag | Description |
|------|-------------|
| `--start-date DATE` | Sprint start (YYYY-MM-DD) |
| `--end-date DATE` | Sprint end (YYYY-MM-DD) |
| `--jira-url URL` | MCP SSE endpoint for Jira (default: `http://localhost:8080`) |
| `--gitlab-url URL` | MCP SSE endpoint for GitLab (default: `http://localhost:8081`) |
| `--db PATH` | SQLite database path (default: `sprint_data.db`) |
| `--fetch-only` | Ingest data without reconciling |
| `--reconcile-only` | Reconcile from existing DB without re-fetching |
| `-o, --output PATH` | Write report to file (default: stdout) |
| `-v, --verbose` | Debug logging |

## Reconciler rules

For each ticket within the date range:

| Step | Condition | Flag | Points |
|------|-----------|------|--------|
| 1 | No events in range | `untouched` | 0 |
| 2 | No gate + Not Done | `in-flight` | 0 |
| 3 | No gate + Done | `no-gate` | 0 |
| 4 | Regression after last gate | `regressed` | 0 |
| 5 | Final status is Done | `valid` | ticket points |
| 6 | Worked on, not Done | `in-flight` | 0 |

**Gate events** = `code_review_approved` or `mr_merged`.
**Regression** = `reopened` or `returned_to_progress` after the last gate.

## Example output

```markdown
# Sprint Report — Sprint 42

**Period:** 2025-04-01 → 2025-04-14

| # | Ticket | Source | Final Status | Points | Flag |
|---|--------|--------|--------------|--------|------|
| 1 | `PROJ-100` | jira | Done | 5.0 | ✅ valid |
| 2 | `PROJ-101` | jira | In Progress | 0.0 | ❌ regressed |
| 3 | `PROJ-102` | jira | Code Review | 0.0 | 🔄 in-flight |
| 4 | `!45` | gitlab | merged | 3.0 | ✅ valid |

## Summary

| Metric | Value |
|--------|-------|
| **Sprint Velocity** | **8.0** |
```

## Extending

Add custom Jira/GitLab workflow statuses to `database.py`:

```python
JIRA_DONE_STATUSES = {"Done", "Closed", "Resolved", "Complete", "Merged"}
REOPEN_WORDS = {"Reopened", "Reopen", "Return to Progress"}
GATE_TRANSITIONS = {"code review approved", "review approved", ...}
```

Modify `_classify_gitlab_event()` in `database.py` for GitLab-specific state
transitions.

## Deterministic guarantee

Same raw data + same date range = same output every time. Reports are
cached in the `sprint_report` table so re-runs with `--reconcile-only`
hit the DB without touching MCP servers.
