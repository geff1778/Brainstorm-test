"""Command-line interface for the orchestration platform.

Usage::

    python -m ohp init
    python -m ohp submit "Fix the login bug" --project brainstorm
    python -m ohp run --drain
    python -m ohp status
    python -m ohp show task_abc123
    python -m ohp approve task_abc123
    python -m ohp cancel task_abc123
    python -m ohp recover
    python -m ohp projects
"""

from __future__ import annotations

import argparse
import json
import sys

from .agent_server import AgentServerClient
from .config import Settings
from .dispatcher import Dispatcher
from .logging_setup import setup_logging
from .models import Project, TaskStatus
from .store import Store


def _build(args: argparse.Namespace) -> tuple[Settings, Store, Dispatcher]:
    settings = Settings()
    if getattr(args, "db", None):
        settings.db_path = args.db
    settings.ensure_dirs()
    store = Store(settings.db_path)
    dispatcher = Dispatcher(store, settings)
    return settings, store, dispatcher


def _print(obj: object) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_init(args: argparse.Namespace) -> int:
    settings, store, _ = _build(args)
    settings.ensure_dirs()
    _print({"ok": True, "db": settings.db_path, "schema_version": "1"})
    store.close()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report whether the environment is ready to dispatch work."""
    settings, store, dispatcher = _build(args)
    client = AgentServerClient(settings.agent_server_url, settings.session_api_key)
    report: dict[str, object] = {
        "db_path": settings.db_path,
        "db_exists": bool(settings.db_path),
        "agent_server_url": settings.agent_server_url,
        "session_key_present": bool(settings.session_api_key),
    }
    try:
        info = client.server_info()
        report["agent_server"] = {
            "available": True,
            "version": info.get("version"),
            "usable_tools": info.get("usable_tools"),
        }
    except Exception as exc:  # noqa: BLE001 - report any failure verbatim
        report["agent_server"] = {"available": False, "error": str(exc)}
    report["cloud_key_present"] = bool(settings.cloud_api_key)
    report["counts"] = store.counts_by_status()
    _print(report)
    store.close()
    return 0


def cmd_submit(args: argparse.Namespace) -> int:
    _, store, dispatcher = _build(args)
    subtasks = json.loads(args.subtasks) if args.subtasks else None
    task, created = dispatcher.submit(
        args.request,
        project=args.project,
        title=args.title,
        idempotency_key=args.idempotency_key,
        subtasks=subtasks,
        context=args.context or "",
    )
    _print({"created": created, "task": task.to_dict()})
    store.close()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    _, store, dispatcher = _build(args)
    if args.task_id:
        task = dispatcher.run(args.task_id)
        _print(task.to_dict())
    else:
        done = dispatcher.run_pending(max_tasks=args.max)
        _print([t.to_dict() for t in done])
    store.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    _, store, dispatcher = _build(args)
    report = dispatcher.status()
    report["queued"] = [
        {"task_id": t.task_id, "title": t.title, "priority": t.priority.value}
        for t in store.list_tasks(status=TaskStatus.QUEUED)
    ]
    report["blocked"] = [
        {"task_id": t.task_id, "title": t.title, "reason": t.blocked_reason}
        for t in store.list_tasks(status=TaskStatus.BLOCKED)
    ]
    _print(report)
    store.close()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    _, store, _ = _build(args)
    task = store.get_task(args.task_id)
    if task is None:
        print(f"no such task: {args.task_id}", file=sys.stderr)
        store.close()
        return 1
    _print(
        {
            "task": task.to_dict(),
            "events": [e.__dict__ for e in store.list_events(task.task_id)],
        }
    )
    store.close()
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    _, store, dispatcher = _build(args)
    task = dispatcher.approve(args.task_id)
    _print(task.to_dict())
    store.close()
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    _, store, dispatcher = _build(args)
    task = dispatcher.cancel(args.task_id)
    _print(task.to_dict())
    store.close()
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    _, store, dispatcher = _build(args)
    pending = store.list_tasks(status=TaskStatus.OUTCOME_UNKNOWN)
    results = []
    for task in pending:
        results.append(dispatcher.run(task.task_id).to_dict())
    _print({"reconciled": len(results), "tasks": results})
    store.close()
    return 0


def cmd_projects(args: argparse.Namespace) -> int:
    _, store, _ = _build(args)
    if args.register:
        project = Project(
            name=args.register,
            repo=args.repo or "",
            working_dir=args.working_dir or "",
            default_branch=args.branch or "main",
            stack=(args.stack or "").split(",") if args.stack else [],
            build_cmd=args.build or "",
            test_cmd=args.test or "",
        )
        store.upsert_project(project)
        _print({"registered": project.name, "working_dir": project.working_dir})
    else:
        _print([p.__dict__ for p in store.list_projects()])
    store.close()
    return 0


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ohp", description="OpenHands orchestration platform"
    )
    parser.add_argument("--db", help="path to the SQLite database", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database").set_defaults(func=cmd_init)
    sub.add_parser("doctor", help="check environment readiness").set_defaults(func=cmd_doctor)

    p_submit = sub.add_parser("submit", help="classify and enqueue a request")
    p_submit.add_argument("request")
    p_submit.add_argument("--project", default=None)
    p_submit.add_argument("--title", default=None)
    p_submit.add_argument("--idempotency-key", default=None)
    p_submit.add_argument("--context", default=None)
    p_submit.add_argument("--subtasks", default=None, help="JSON list of subtask specs")
    p_submit.set_defaults(func=cmd_submit)

    p_run = sub.add_parser("run", help="execute a task or drain the queue")
    p_run.add_argument("task_id", nargs="?", default=None)
    p_run.add_argument("--max", type=int, default=50)
    p_run.set_defaults(func=cmd_run)

    sub.add_parser("status", help="summary of task states").set_defaults(func=cmd_status)

    p_show = sub.add_parser("show", help="show a task and its event log")
    p_show.add_argument("task_id")
    p_show.set_defaults(func=cmd_show)

    p_approve = sub.add_parser("approve", help="approve a task awaiting approval")
    p_approve.add_argument("task_id")
    p_approve.set_defaults(func=cmd_approve)

    p_cancel = sub.add_parser("cancel", help="cancel a task")
    p_cancel.add_argument("task_id")
    p_cancel.set_defaults(func=cmd_cancel)

    sub.add_parser("recover", help="reconcile outcome_unknown tasks").set_defaults(
        func=cmd_recover
    )

    p_projects = sub.add_parser("projects", help="list or register projects")
    p_projects.add_argument("--register", default=None, help="project name")
    p_projects.add_argument("--repo", default=None)
    p_projects.add_argument("--working-dir", dest="working_dir", default=None)
    p_projects.add_argument("--branch", default=None)
    p_projects.add_argument("--stack", default=None)
    p_projects.add_argument("--build", default=None)
    p_projects.add_argument("--test", default=None)
    p_projects.set_defaults(func=cmd_projects)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
