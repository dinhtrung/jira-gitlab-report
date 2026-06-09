# AGENTS.md — AI agent instructions for jira-gitlab-report

## Project identity

**jira-gitlab-report** is a local Python CLI utility that generates
deterministic sprint performance reports. It pulls changelog and MR
event data from Jira and GitLab MCP servers (Dockerized, SSE transport),
reconciles the timeline via a state machine, and outputs a Markdown
table with sprint velocity metrics.

## Architecture

```
jira-gitlab-report/
├── main.py            # CLI entry point: arg parsing, orchestration, Markdown rendering
├── sse_client.py      # Async MCP Streamable HTTP/SSE client (httpx)
├── database.py        # SQLite schema, CRUD, raw→normalized event pipeline
├── reconciler.py      # Deterministic state machine: ticket → valid/in-flight/regressed
├── schema.sql         # DDL for raw_jira_events, raw_gitlab_events, ticket_history, sprint_report
├── pyproject.toml     # Metadata + httpx dependency
├── AGENTS.md          # This file
```

## Key design decisions

- **SQLite for persistence** — enables repeatable, deterministic re-runs
  without re-fetching from MCP servers.
- **State-machine reconciler** — `reconciler.py::Reconciler._judge()` applies
  rules in a fixed order so the same input always yields the same output.
- **MCP SSE transport** — `sse_client.py::MCPSession` implements the
  Streamable HTTP transport: GET for SSE, POST to the discovered endpoint
  for JSON-RPC calls.

## Reconciler rules (the "deterministic" contract)

For each ticket within `--start-date` … `--end-date`:

| Step | Condition | Flag | Points |
|------|-----------|------|--------|
| 1 | No events in range | `untouched` | 0 |
| 2 | No gate event + Not Done | `in-flight` | 0 |
| 2 | No gate event + Done | `no-gate` | 0 |
| 3 | Regression after last gate | `regressed` | 0 |
| 4 | Final status is Done | `valid` | ticket points |
| 5 | Worked on, not Done | `in-flight` | 0 |

**Gate events** = `code_review_approved` or `mr_merged`.
**Regression** = `reopened` or `returned_to_progress` after the last gate.

## Extending

- Add Jira status keywords to `database.py` → `GATE_TRANSITIONS`, `REOPEN_WORDS`,
  `JIRA_DONE_STATUSES`.
- Add GitLab workflow states to `_classify_gitlab_event()`.

## Running

```bash
python main.py --start-date 2025-04-01 --end-date 2025-04-14 \
    --jira-url http://localhost:8080 --gitlab-url http://localhost:8081
```

Modes: `--fetch-only` (ingest only), `--reconcile-only` (from existing DB),
`-o report.md` (write to file).
