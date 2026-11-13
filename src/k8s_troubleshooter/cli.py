"""The interface. Commands: investigate, eval, approve, trace, logs, scenario apply, scenario delete."""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from k8s_troubleshooter.agent.graph import investigate
from k8s_troubleshooter.agent.remediation import approve, save_proposal
from k8s_troubleshooter.clients.kubectl import run
from k8s_troubleshooter.clients.kubernetes import list_pods
from k8s_troubleshooter.config import log_file, max_steps, project_root
from k8s_troubleshooter.evals.graders import grade
from k8s_troubleshooter.evals.scenarios import SCENARIOS
from k8s_troubleshooter.exceptions import CommandError, SafetyError
from k8s_troubleshooter.observability.log_file import follow, format_entry, read_entries, select
from k8s_troubleshooter.observability.logs import event, run_logs, setup_logging
from k8s_troubleshooter.observability.trace_html import render_html
from k8s_troubleshooter.observability.trace_view import list_runs, report, table, trace_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="k8s-troubleshooter")
    commands = parser.add_subparsers(dest="command", required=True)
    ask = commands.add_parser("investigate", help="ask a free-text question")
    ask.add_argument("question")
    commands.add_parser("eval", help="investigate the three scenarios with the configured model and grade each answer")
    yes = commands.add_parser("approve", help="apply one saved health-check change")
    yes.add_argument("proposal_id")
    show = commands.add_parser("trace", help="show one run as a readable report: latest, a run id, or list")
    show.add_argument("run_id", nargs="?", default="latest")
    view = show.add_mutually_exclusive_group()
    view.add_argument("--table", action="store_true", help="one line per step instead of the full JSON report")
    view.add_argument("--html", action="store_true", help="save the run as a page to open in a browser")
    scenario = commands.add_parser("scenario", help="apply or remove a saved failure")
    scenario.add_argument("action", choices=("apply", "delete"))
    scenario.add_argument("name")
    logs = commands.add_parser("logs", help="every command's log lines, from both containers, newest last")
    logs.add_argument("--run", default="", help="only one run, by its run_id (or approve-<proposal id>)")
    logs.add_argument("--level", default="", help="this level and above: debug, info, warning, error")
    logs.add_argument("--event", default="", help="only one event, such as tool_call or run_finished")
    logs.add_argument("--service", default="", choices=("", "agent", "admin", "local"), help="only one container")
    logs.add_argument("--since", default="", help="only newer lines, such as 30m, 2h, or 1d")
    logs.add_argument("--limit", type=int, default=100, help="how many of the newest lines (default 100)")
    logs.add_argument("--json", action="store_true", help="the raw JSON lines")
    logs.add_argument("--follow", action="store_true", help="keep printing new lines, like tail -f")
    args = parser.parse_args(argv)
    setup_logging(args.command)
    try:
        if args.command == "investigate":
            result = investigate(args.question)
            findings = result.get("findings")
            result["proposal_id"] = save_proposal(findings, result["run_id"], result["trace_id"], result.pop("root_span_id")) if findings else None
            if result["proposal_id"]:
                with run_logs(result["run_id"]):
                    event("fix_proposed", proposal_id=result["proposal_id"])
            print(json.dumps(result, indent=2))
            return 0 if result["stopped_reason"] == "conclusion" else 2
        if args.command == "eval":
            report = run_evals()
            print(json.dumps(report, indent=2))
            return 0 if report["passed"] == report["total"] else 1
        if args.command == "approve":
            print(json.dumps(approve(args.proposal_id), indent=2))
            return 0
        if args.command == "trace":
            print(_save_page(args.run_id) if args.html else _show_trace(args.run_id, args.table))
            return 0
        if args.command == "logs":
            return _show_logs(args)
        changed = _scenario(args.action, args.name)
        event("scenario_changed", scenario=args.name, action=args.action, changed=changed)
        print(json.dumps({"scenario": args.name, "action": args.action}))
        return 0
    except (SafetyError, CommandError, ValueError, FileNotFoundError, OSError) as exc:
        event("command_failed", logging.ERROR, command=args.command, error_type=type(exc).__name__, error=str(exc))
        return 2


def run_evals() -> dict:
    """Read-only: the scenarios must already be applied by admin."""
    graded = []
    for scenario in SCENARIOS:
        if not [pod for pod in list_pods(scenario["namespace"])["pods"] if pod["app"] == scenario["app"]]:
            found = [f"scenario not applied: docker compose run --rm admin scenario apply {scenario['id']}"]
            graded.append({"scenario": scenario["id"], "passed": False, "problems": found, "trace_path": None})
            continue
        try:
            wait_for_app(scenario["namespace"], scenario["app"], scenario["settled"])
        except TimeoutError as exc:
            graded.append({"scenario": scenario["id"], "passed": False, "problems": [str(exc)], "trace_path": None})
            continue
        result = investigate(scenario["question"])
        found = grade(result, scenario, max_steps=max_steps())
        event("eval_scenario", logging.INFO if not found else logging.WARNING, scenario=scenario["id"], passed=not found, problems=found, investigation_run_id=result["run_id"])
        graded.append({"scenario": scenario["id"], "passed": not found, "problems": found, "trace_path": result["trace_path"]})
    return {"passed": sum(item["passed"] for item in graded), "total": len(graded), "scenarios": graded}


def _show_trace(run_id: str, as_table: bool) -> str:
    if run_id == "list":
        return _runs_table(list_runs()) if as_table else json.dumps(list_runs(), indent=2, ensure_ascii=False)
    body = report(trace_path(run_id))
    return table(body) if as_table else json.dumps(body, indent=2, ensure_ascii=False)


def _show_logs(args) -> int:
    filters = {"run": args.run, "level": args.level, "name": args.event, "service": args.service, "since": args.since}

    def show(entry: dict) -> None:
        print(json.dumps(entry) if args.json else format_entry(entry), flush=True)

    entries = select(read_entries(), **filters)
    if not entries and not args.follow:
        print("No log lines match." if any(filters.values()) else f"No log lines yet in {log_file()}.")
    for entry in entries[-max(1, args.limit):]:
        show(entry)
    if args.follow:
        try:
            follow(show, **filters)
        except KeyboardInterrupt:
            pass
    return 0


def _save_page(run_id: str) -> str:
    if run_id == "list":
        raise ValueError("--html shows one run: give latest, a run id, or approve-<proposal id>")
    path = trace_path(run_id)
    page = path.with_suffix(".html")
    page.write_text(render_html(report(path)), encoding="utf-8")
    return f"Saved {page.parent.name}/{page.name}. Open it in a browser."


def _runs_table(runs: list[dict]) -> str:
    lines = ["Newest first:", "", "  Run id                                    Took     Result         Question"]
    for item in runs:
        took = f"{item['duration_ms'] / 1000:.1f} s" if "duration_ms" in item else ""
        lines.append(f"  {item['run_id']:<40}  {took:>7}  {item.get('result', ''):<13}  {item.get('question', '')}")
    return "\n".join(lines)


def apply_scenario(name: str) -> list[str]:
    """kubectl's own lines, such as `deployment.apps/crashing-app created`."""
    return _lines(run(["apply", "-f", str(_scenario_dir(name))]).stdout)


def delete_scenario(name: str) -> list[str]:
    return _lines(run(["delete", "-f", str(_scenario_dir(name)), "--ignore-not-found=true"]).stdout)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def wait_for_app(namespace: str, app: str, ready, timeout: float = 180) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        live = [pod for pod in list_pods(namespace)["pods"] if pod["app"] == app and not pod.get("deleting")]
        last = live[-1] if live else None
        match = next((pod for pod in live if ready(pod)), None)
        if match is not None:
            return match
        time.sleep(2)
    raise TimeoutError(f"{namespace}/{app} did not reach the expected state; last={last}")


def _scenario(action: str, name: str) -> list[str]:
    return apply_scenario(name) if action == "apply" else delete_scenario(name)


def _scenario_dir(name: str) -> Path:
    path = project_root() / "scenarios" / name
    if not path.is_dir():
        raise FileNotFoundError(f"unknown scenario: {name}")
    return path
