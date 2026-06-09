# TODO — jira-gitlab-report

Tasks to improve the Deterministic Sprint Reporting Engine.

## High Priority (Accuracy & Speed)

- [x] **Fix Points Logic** (`reconciler.py::Reconciler._extract_points`)
    - Currently sums all `points_assigned` events.
    - Should be changed to "last known value" at the end of the reporting period.
- [x] **Parallel GitLab Ingestion** (`main.py::fetch_gitlab_data`)
    - Currently fetches MR details one-by-one ($N+1$ calls).
    - Use `asyncio.gather` with a semaphore to fetch events in parallel.
- [x] **Improve Jira Pagination Stability** (`main.py::fetch_jira_data`)
    - Search pagination can skip issues if they move during the fetch.
    - Consider sorting by `key` or using a more robust pagination strategy if the MCP tool supports it.

## Medium Priority (Flexibility & Reliability)

- [x] **External Configuration**
    - Move hardcoded Jira/GitLab statuses (`JIRA_DONE_STATUSES`, `REOPEN_WORDS`, `GATE_TRANSITIONS`, `POINTS_FIELD_NAMES` in `database.py`) to a `config.yaml` or `.env`.
- [x] **Add Regression Count** (`reconciler.py`, `database.py`)
    - Instead of a binary `regressed` flag, track the actual count of reopen events after the last gate.
- [x] **Consistency in Log Formats**
    - Audit `logging` calls across all modules to ensure consistent metadata (timestamps, levels).
- [x] **Date/Time Zone Awareness**
    - Ensure all ISO-8601 strings are handled with consistent timezone awareness (UTC preferred) to avoid boundary issues.

## Low Priority (Features & Refactoring)

- [x] **Cycle Time Metrics**
    - Calculate time spent in each state transition to provide "Average Time to Done".
- [x] **Alternative Output Formats**
    - Add `--format csv` and `--format json` to `main.py`.
- [x] **Automated Tests**
    - Add a `tests/` directory with `pytest` for the `Reconciler` state machine and `Database` normalization.
- [ ] **Sprint Auto-detection**
    - Attempt to discover the "Active" sprint from Jira if `--start-date` and `--end-date` are missing.
- [ ] **Type Refinement**
    - Replace raw `dict` usage for API responses with Pydantic models or `TypedDict` for better IDE support and runtime validation.
