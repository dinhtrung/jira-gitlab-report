### Project Review and Suggestions for `jira-gitlab-report`

After reviewing the codebase, I have identified several areas for improvement, ranging from logic refinements to performance optimizations and architectural enhancements.

#### 1. Points Attribution Logic Refinement
In `reconciler.py::Reconciler._extract_points`, the current implementation sums all `points_assigned` events:
```python
@staticmethod
def _extract_points(timeline: list[dict]) -> float:
    pts = 0.0
    for e in timeline:
        if e["event_type"] == "points_assigned":
            pts += e.get("points", 0.0)
    return pts
```
**Issue:** If a ticket's points are changed from 3 to 5, the total would incorrectly show 8 points.
**Suggestion:** Change this to follow the "last known value" pattern or track the most recent assignment before the `end_date`.

#### 2. GitLab Ingestion Performance
In `main.py::fetch_gitlab_data`, the script fetches a list of MRs and then iterates through them to fetch detailed events for each MR individually.
**Issue:** This results in $N+1$ network calls, which can be extremely slow for large sprints with many MRs.
**Suggestion:** Check if the GitLab MCP server supports bulk fetching of events or use concurrent `asyncio` tasks to fetch MR details in parallel (with rate limiting).

#### 3. Error Resilience and Data Integrity
*   **Jira Search Pagination:** The loop in `fetch_jira_data` uses `startAt` and `maxResults`. If the underlying data changes during fetching (e.g., an issue is updated), some issues might be skipped or duplicated.
*   **Database Constraints:** `schema.sql` uses `INSERT OR REPLACE` in `sprint_report`. While this ensures determinism on re-runs, it might mask bugs where different runs produce conflicting data for the same ticket.

#### 4. Enhancing the "Deterministic" Contract
The `reconciler.py` defines a "Regression" as a reopen after the last gate.
**Suggestion:** Add a `regression_count` to the report. Currently, it's a binary "regressed" flag. Knowing *how many times* a ticket regressed provides better insight into sprint quality.

#### 5. Architectural Improvements
*   **Configuration Management:** Most Jira/GitLab statuses and keywords are hardcoded in `database.py`. Moving these to a `config.yaml` or a `.env` file would make the tool easier to adapt to different team workflows without modifying code.
*   **Type Safety:** While type hints are used, some parts of the data processing (like `_extract_json` in `main.py`) rely on loose `dict` types. Introducing Pydantic models for Jira/GitLab responses would improve maintainability.

#### 6. Proposed New Features
*   **Sprint Name Auto-detection:** Currently, `--sprint-name` is often required or relies on Jira field data. Adding a feature to auto-detect the current active sprint would improve UX.
*   **CSV/JSON Output:** In addition to Markdown, support for CSV or JSON output would allow for easier integration with other data analysis tools (like Excel or PowerBI).
*   **Transition Analysis:** Track the time spent in each status (Lead Time / Cycle Time) by calculating the delta between `status_change` events in `ticket_history`.

#### Summary of Priorities
1.  **Fix Points Logic** (Critical for accuracy).
2.  **Parallel GitLab Fetching** (Critical for usability/speed).
3.  **External Configuration** (Important for flexibility).
4.  **Cycle Time Metrics** (High value-add for reporting).