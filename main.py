"""
main.py — CLI entry point for the Deterministic Sprint Reporting Engine.

Usage::

    python main.py \\
        --start-date 2025-04-01 \\
        --end-date   2025-04-14 \\
        --jira-url   http://localhost:8080 \\
        --gitlab-url http://localhost:8081 \\
        --db         sprint_data.db
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from database import Database
from reconciler import Reconciler, SprintReport
from sse_client import MCPSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data-fetch orchestration
# ---------------------------------------------------------------------------


async def fetch_jira_data(
    jira: MCPSession,
    db: Database,
    start_date: str,
    end_date: str,
    jql_extra: str = "",
) -> int:
    """Query Jira for issues updated in the window and ingest their changelogs."""
    logger.info("Fetching Jira data …")
    tools = await jira.list_tools()
    tool_names = {t.name for t in tools}
    logger.debug("Jira tools: %s", tool_names)

    jql = f"updated >= '{start_date}' AND updated <= '{end_date}' ORDER BY key ASC"
    if jql_extra:
        jql += f" AND {jql_extra}"

    search_tool = _pick_tool(
        tool_names, ["jira_search", "search_issues", "jira_search_issues", "search"]
    )

    total = 0
    start_at = 0
    max_results = 50

    while True:
        tool_obj = next((t for t in tools if t.name == search_tool), None)
        input_schema = tool_obj.input_schema if tool_obj else {}
        properties = input_schema.get("properties", {})

        args: dict[str, Any] = {"jql": jql}
        if "start_at" in properties:
            args["start_at"] = start_at
        else:
            args["startAt"] = start_at

        if "limit" in properties:
            args["limit"] = max_results
        elif "maxResults" in properties:
            args["maxResults"] = max_results
        else:
            args["limit"] = max_results

        if "expand" in properties:
            prop_type = properties["expand"].get("type", "string")
            if prop_type == "array":
                args["expand"] = ["changelog"]
            else:
                args["expand"] = "changelog"

        result = await jira.call_tool(search_tool, args)
        if result.is_error:
            logger.error("Jira search error: %s", result.content)
            break

        data = _extract_json(result.content)
        issues = data.get("issues", [])
        if not issues:
            break

        db.parse_and_ingest_jira_search_result(issues)
        total += len(issues)

        returned_total = data.get("total", 0)
        actual_start_at = data.get("start_at", data.get("startAt", start_at))
        start_at = actual_start_at + len(issues)

        if start_at >= returned_total:
            break

    logger.info("Ingested %d Jira issues", total)
    return total


async def fetch_gitlab_data(
    gitlab: MCPSession,
    db: Database,
    start_date: str,
    end_date: str,
    project_id: int = 0,
) -> int:
    """Query GitLab for MRs updated in the window and ingest their state events."""
    logger.info("Fetching GitLab data …")
    tools = await gitlab.list_tools()
    tool_names = {t.name for t in tools}
    logger.debug("GitLab tools: %s", tool_names)

    list_tool = _pick_tool(
        tool_names, ["list_merge_requests", "gitlab_list_merge_requests", "search_merge_requests"]
    )
    detail_tool = _pick_tool(tool_names, ["get_merge_request", "gitlab_get_merge_request"])

    list_tool_obj = next((t for t in tools if t.name == list_tool), None)
    list_input_schema = list_tool_obj.input_schema if list_tool_obj else {}
    list_properties = list_input_schema.get("properties", {})
    list_pid_type = list_properties.get("project_id", {}).get("type", "string")

    list_args: dict = {
        "updated_after": f"{start_date}T00:00:00Z",
        "updated_before": f"{end_date}T23:59:59Z",
        "state": "all",
        "per_page": 100,
    }
    if project_id:
        if list_pid_type == "string":
            list_args["project_id"] = str(project_id)
        else:
            list_args["project_id"] = project_id

    result = await gitlab.call_tool(list_tool, list_args)
    if result.is_error:
        logger.error("GitLab list error: %s", result.content)
        return 0

    mrs = _extract_json(result.content)
    if isinstance(mrs, dict):
        mrs = mrs.get("data", mrs.get("items", []))

    if not isinstance(mrs, list):
        logger.warning("Unexpected GitLab response shape: %s", type(mrs))
        return 0

    total = 0
    semaphore = asyncio.Semaphore(8)  # max concurrent MR detail fetches

    # Check detail tool parameters
    detail_tool_obj = next((t for t in tools if t.name == detail_tool), None)
    detail_input_schema = detail_tool_obj.input_schema if detail_tool_obj else {}
    detail_properties = detail_input_schema.get("properties", {})
    iid_type = detail_properties.get("merge_request_iid", {}).get("type", "string")
    pid_type = detail_properties.get("project_id", {}).get("type", "string")

    async def _fetch_one_mr(mr_summary: dict) -> int:
        mr_iid = mr_summary.get("iid")
        if not mr_iid:
            return 0
        pid = mr_summary.get("project_id", project_id)

        detail_args: dict[str, Any] = {}
        if iid_type == "string":
            detail_args["merge_request_iid"] = str(mr_iid)
        else:
            detail_args["merge_request_iid"] = mr_iid

        if pid:
            if pid_type == "string":
                detail_args["project_id"] = str(pid)
            else:
                detail_args["project_id"] = pid

        async with semaphore:
            detail = await gitlab.call_tool(detail_tool, detail_args)

        if detail.is_error:
            logger.warning("GitLab detail error for !%s: %s", mr_iid, detail.content)
            db.parse_and_ingest_gitlab_mr_events([mr_summary], project_id=pid)
            return 1

        full_mr = _extract_json(detail.content)
        if isinstance(full_mr, dict):
            db.parse_and_ingest_gitlab_mr_events([full_mr], project_id=pid)
            return 1
        if isinstance(full_mr, list):
            db.parse_and_ingest_gitlab_mr_events(full_mr, project_id=pid)
            return len(full_mr)
        return 0

    results = await asyncio.gather(*[_fetch_one_mr(mr) for mr in mrs], return_exceptions=True)
    for r in results:
        if isinstance(r, int):
            total += r
        elif isinstance(r, Exception):
            logger.warning("GitLab fetch task failed: %s", r)

    logger.info("Ingested %d GitLab MRs", total)
    return total


# ---------------------------------------------------------------------------
# Markdown report renderer
# ---------------------------------------------------------------------------


def render_markdown(report: SprintReport) -> str:
    """Produce a clean Markdown sprint-performance table."""

    lines: list[str] = [
        f"# Sprint Report — {report.sprint_name or 'Unnamed'}",
        "",
        f"**Period:** {report.start_date} → {report.end_date}",
        "",
        "| # | Ticket | Source | Final Status | Points | Flag |",
        "|---|--------|--------|--------------|--------|------|",
    ]

    for idx, t in enumerate(report.tickets, start=1):
        flag_icon = _flag_icon(t.flag)
        lines.append(
            f"| {idx} | `{t.ticket_id}` | {t.source} | {t.final_status} | "
            f"{t.points} | {flag_icon} {t.flag} |"
        )

    lines += [
        "",
        "---",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| **Total tickets** | {len(report.tickets)} |",
        f"| **Valid (points earned)** | {report.valid_count} |",
        f"| **In-Flight (0 pts)** | {report.in_flight_count} |",
        f"| **Regressed (0 pts)** | {report.regressed_count} |",
        f"| **░ Total regressions** | {report.total_regressions} |",
        f"| **Sprint Velocity** | **{report.total_points}** |",
        "",
        "---",
        "",
        "## Details",
        "",
    ]

    for t in report.tickets:
        if t.detail:
            lines.append(f"- **{t.ticket_id}** ({t.flag}): {t.detail}")
        if t.cycle_time_hours:
            parts = [f"{s}: {h:.1f}h" for s, h in sorted(t.cycle_time_hours.items())]
            lines.append(f"  ⏱ {', '.join(parts)}")

    return "\n".join(lines)


def _flag_icon(flag: str) -> str:
    return {
        "valid": "✅",
        "in-flight": "🔄",
        "regressed": "❌",
        "no-gate": "⚠️",
        "not-done": "⏳",
        "untouched": "⬜",
    }.get(flag, "❓")


def render_csv(report: SprintReport) -> str:
    """Produce a CSV sprint-performance table."""
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        [
            "#",
            "Ticket",
            "Source",
            "Final Status",
            "Points",
            "Flag",
            "Regressions",
            "Cycle Time",
            "Detail",
        ]
    )
    for idx, t in enumerate(report.tickets, start=1):
        ct = "; ".join(f"{s}:{h:.1f}h" for s, h in sorted(t.cycle_time_hours.items()))
        w.writerow(
            [
                idx,
                t.ticket_id,
                t.source,
                t.final_status,
                t.points,
                t.flag,
                t.regression_count,
                ct,
                t.detail,
            ]
        )
    w.writerow([])
    w.writerow(["Summary", "Value"])
    w.writerow(["Total tickets", len(report.tickets)])
    w.writerow(["Valid (points earned)", report.valid_count])
    w.writerow(["In-Flight (0 pts)", report.in_flight_count])
    w.writerow(["Regressed (0 pts)", report.regressed_count])
    w.writerow(["Total regressions", report.total_regressions])
    w.writerow(["Sprint Velocity", report.total_points])
    return buf.getvalue()


def render_json(report: SprintReport) -> str:
    """Produce a JSON sprint-performance report."""
    return json.dumps(
        {
            "sprint_name": report.sprint_name,
            "start_date": report.start_date,
            "end_date": report.end_date,
            "tickets": [
                {
                    "ticket_id": t.ticket_id,
                    "source": t.source,
                    "final_status": t.final_status,
                    "points": t.points,
                    "flag": t.flag,
                    "regression_count": t.regression_count,
                    "cycle_time_hours": t.cycle_time_hours,
                    "detail": t.detail,
                }
                for t in report.tickets
            ],
            "summary": {
                "total_tickets": len(report.tickets),
                "valid": report.valid_count,
                "in_flight": report.in_flight_count,
                "regressed": report.regressed_count,
                "total_regressions": report.total_regressions,
                "sprint_velocity": report.total_points,
            },
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pick_tool(available: set[str], candidates: list[str]) -> str:
    for name in candidates:
        if name in available:
            return name
    if available:
        return next(iter(available))
    raise RuntimeError("No tools available on the MCP server")


def _extract_json(content: list[dict]) -> Any:
    """Unpack an MCP tool call result into a Python object."""
    if not isinstance(content, list):
        return {}
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            raw: str = block.get("text", "")
            if not raw:
                continue
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
        if block.get("type") == "resource":
            resource: dict = block.get("resource", {})
            raw = resource.get("text", resource.get("blob", ""))
            if isinstance(raw, str):
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw
            return raw
    return {}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sprint-report",
        description="Deterministic Sprint Reporting Engine",
    )
    p.add_argument("--start-date", required=True, help="Sprint start (YYYY-MM-DD)")
    p.add_argument("--end-date", required=True, help="Sprint end (YYYY-MM-DD)")
    p.add_argument(
        "--jira-url",
        default="http://localhost:8080",
        help="MCP SSE endpoint for Jira server",
    )
    p.add_argument(
        "--gitlab-url",
        default="http://localhost:8081",
        help="MCP SSE endpoint for GitLab server",
    )
    p.add_argument(
        "--db",
        default="sprint_data.db",
        help="Path to SQLite database (default: sprint_data.db)",
    )
    p.add_argument(
        "--fetch-only",
        action="store_true",
        help="Fetch data without reconciling",
    )
    p.add_argument(
        "--reconcile-only",
        action="store_true",
        help="Reconcile from existing DB without re-fetching",
    )
    p.add_argument(
        "-o",
        "--output",
        default="",
        help="Write report to file (default: stdout)",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose logging",
    )
    p.add_argument(
        "-f",
        "--format",
        default="markdown",
        choices=["markdown", "csv", "json"],
        help="Output format (default: markdown)",
    )
    p.add_argument(
        "--author",
        default="",
        help="Filter report by author (username or email)",
    )
    return p


async def async_main(args: argparse.Namespace) -> None:
    db = Database(args.db)
    db.init()

    if not args.reconcile_only:
        async with MCPSession(args.jira_url) as jira_session:
            await jira_session.initialize()
            await fetch_jira_data(jira_session, db, args.start_date, args.end_date)

        async with MCPSession(args.gitlab_url) as gitlab_session:
            await gitlab_session.initialize()
            await fetch_gitlab_data(gitlab_session, db, args.start_date, args.end_date)

    if args.fetch_only:
        logger.info("Fetch-only mode — skipping reconciliation.")
        db.close()
        return

    db.normalise()
    reconciler = Reconciler(db)
    report = reconciler.reconcile(args.start_date, args.end_date, author_id=args.author or None)

    if args.format == "csv":
        out = render_csv(report)
    elif args.format == "json":
        out = render_json(report)
    else:
        out = render_markdown(report)

    if args.output:
        Path(args.output).write_text(out, encoding="utf-8")
        print(f"Report written to {args.output}")
    else:
        print(out)

    db.close()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception:
        logger.exception("Fatal error")
        sys.exit(1)


if __name__ == "__main__":
    main()
