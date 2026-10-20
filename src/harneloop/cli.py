from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .candidate import (
    CANDIDATE_PLANES,
    VALIDATION_TIERS,
    create_candidate,
    list_candidates,
    read_candidate,
    rebase_candidate,
    set_candidate_status,
)
from .diagnostics import run_doctor
from .environment import ENVIRONMENT_MODES, INTERACTION_MODES, connect_environment, render_environment_status
from .errors import HarneloopError
from .benchmarks import create_benchmark, read_benchmark, record_benchmark_result, compare_benchmark
from .refinement import compose_candidates, add_lesson, list_lessons
from .evidence import add_evidence
from .intake import (
    ACKNOWLEDGEMENT_BASES,
    FIELD_STATUSES,
    INTAKE_FIELDS,
    acknowledge_intake,
    read_intake,
    render_intake_markdown,
    resolve_intake_field,
)
from .adapters import SUPPORTED_ADAPTERS, export_unit
from .attempts import (
    VALID_CONCLUSION_DECISIONS,
    VALID_CONCLUSION_OUTCOMES,
    VALID_CONFIDENCE,
    add_attempt_observation,
    conclude_attempt,
    create_attempt_plan,
)
from .onboarding import render_onboarding_json, render_onboarding_markdown
from .packaging import package_unit
from .preferences import (
    list_registered_units,
    load_preferences,
    register_unit,
    remove_registered_unit,
    resolve_unit_reference,
    update_preference,
)
from .runs import add_artifact, finish_run, start_run
from .state import build_session_brief_data, mark_active, mark_stopped, mark_waiting, read_state
from .state import render_session_brief_markdown, render_state_markdown
from .target import set_target_brief
from .templates import list_templates
from .unit import init_unit, upgrade_unit_protocol
from .validation import validate_unit
from .versioning import promote_candidate, rollback_unit


def add_unit_reference(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("unit", type=Path, help="Harness unit path, registered ID, or registered name")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="harneloop", description="Harneloop harness-unit lifecycle engine")
    subparsers = parser.add_subparsers(dest="command")

    benchmark = subparsers.add_parser(
        "benchmark", help="Freeze external benchmark conditions and compare artifact-backed results",
        description="External agents and graders produce results; Harneloop checks provenance and paired coverage. Scores are 0 to 1, higher is better.",
        epilog="Workflow and JSON format: https://github.com/OpenRootdev/Harneloop/tree/main/examples/benchmark-evidence",
    )
    benchmark_commands = benchmark.add_subparsers(dest="benchmark_command", required=True)
    for name, help_text in [
        ("create", "Freeze a new JSON manifest; refuses to overwrite"),
        ("show", "Show a verified frozen manifest"),
        ("bind", "Freeze the candidate identity and content before held-out results"),
        ("record", "Attach one JSON result to a finished run and stored artifacts"),
        ("compare", "Verify complete matched held-out trials and report paired outcomes"),
    ]:
        command = benchmark_commands.add_parser(name, help=help_text)
        add_unit_reference(command)
        command.add_argument("benchmark_id", help="Unit-local benchmark identifier")
        if name == "create":
            command.add_argument("--manifest", type=Path, required=True, help="JSON manifest with source/tasks/conditions/grader")
        elif name == "record":
            command.add_argument("--result", type=Path, required=True, help="JSON row including observed_conditions, arm, task_id, trial, run_id, score and artifact_ids")
        elif name in {"bind", "compare"}:
            command.add_argument("--candidate-id", required=True)

    refine = subparsers.add_parser("refine", help="Compose candidate overlays and retrieve evidence-linked lessons")
    refine_commands = refine.add_subparsers(dest="refine_command", required=True)
    compose = refine_commands.add_parser("compose", help="Combine compatible candidates; fresh validation is required")
    add_unit_reference(compose)
    compose.add_argument("--candidate-id", action="append", required=True)
    compose.add_argument("--summary", required=True)
    learn = refine_commands.add_parser("learn", help="Store an observation linked to a finished run artifact")
    add_unit_reference(learn)
    learn.add_argument("--cue", required=True)
    learn.add_argument("--lesson", required=True)
    learn.add_argument("--run-id", required=True)
    learn.add_argument("--artifact-id", required=True)
    lessons = refine_commands.add_parser("lessons", help="Retrieve lessons and recheck their source artifacts")
    add_unit_reference(lessons)
    lessons.add_argument("--query", default="")

    onboard_parser = subparsers.add_parser("onboard", help="Print the new-harness onboarding checklist")
    onboard_parser.add_argument("--format", choices=["markdown", "json"], default="markdown")

    setup_parser = subparsers.add_parser("setup", help="Open the guided interactive setup flow")
    setup_parser.add_argument("--home", type=Path)

    units_parser = subparsers.add_parser("units", help="List and manage registered harness units")
    units_subparsers = units_parser.add_subparsers(dest="units_command", required=True)
    units_list = units_subparsers.add_parser("list", help="List registered harness units")
    units_list.add_argument("--home", type=Path)
    units_register = units_subparsers.add_parser("register", help="Register an existing harness unit")
    units_register.add_argument("unit", type=Path, help="Path to an existing harness unit")
    units_register.add_argument("--home", type=Path)
    units_remove = units_subparsers.add_parser("remove", help="Remove a harness unit from the local registry")
    units_remove.add_argument("unit_id_or_path")
    units_remove.add_argument("--home", type=Path)

    settings_parser = subparsers.add_parser("settings", help="View and update Harneloop preferences")
    settings_subparsers = settings_parser.add_subparsers(dest="settings_command", required=True)
    settings_show = settings_subparsers.add_parser("show", help="Show current preferences")
    settings_show.add_argument("--home", type=Path)
    settings_set = settings_subparsers.add_parser("set", help="Set a preference value")
    settings_set.add_argument("key")
    settings_set.add_argument("value")
    settings_set.add_argument("--home", type=Path)

    init_parser = subparsers.add_parser("init-unit", help="Create a new harness unit")
    init_parser.add_argument("path", type=Path, help="Directory in which to create the harness unit")
    init_parser.add_argument("--id", required=True)
    init_parser.add_argument("--name", required=True)
    init_parser.add_argument("--template", default="blank", choices=list_templates())
    init_parser.add_argument("--home", type=Path, help="Override the Harneloop home used for registration")
    init_parser.add_argument("--no-register", action="store_true", help="Create the unit without registering it")

    upgrade_parser = subparsers.add_parser("upgrade-unit", help="Add missing current protocol files to an existing unit")
    add_unit_reference(upgrade_parser)

    intake_parser = subparsers.add_parser("intake", help="Review and resolve adaptive onboarding context")
    intake_subparsers = intake_parser.add_subparsers(dest="intake_command", required=True)
    intake_status = intake_subparsers.add_parser("status", help="Show confirmed, inferred, and unresolved context")
    add_unit_reference(intake_status)
    intake_status.add_argument("--format", choices=["markdown", "json"], default="markdown")
    intake_resolve = intake_subparsers.add_parser("resolve", help="Record one onboarding context field")
    add_unit_reference(intake_resolve)
    intake_resolve.add_argument("--field", required=True, choices=sorted(INTAKE_FIELDS))
    intake_resolve.add_argument("--value", required=True)
    intake_resolve.add_argument("--status", required=True, choices=sorted(FIELD_STATUSES - {"unknown"}))
    intake_resolve.add_argument("--source", required=True)
    intake_acknowledge = intake_subparsers.add_parser("acknowledge", help="Record user confirmation or delegation")
    add_unit_reference(intake_acknowledge)
    intake_acknowledge.add_argument("--basis", required=True, choices=sorted(ACKNOWLEDGEMENT_BASES))
    intake_acknowledge.add_argument("--note", required=True)

    template_parser = subparsers.add_parser("template", help="Inspect available unit templates")
    template_subparsers = template_parser.add_subparsers(dest="template_command", required=True)
    template_subparsers.add_parser("list", help="List available unit templates")

    target_parser = subparsers.add_parser("target", help="Describe the target task for a harness unit")
    target_subparsers = target_parser.add_subparsers(dest="target_command", required=True)
    target_set = target_subparsers.add_parser("set", help="Set the target task brief")
    add_unit_reference(target_set)
    target_set.add_argument("--task", required=True)
    target_set.add_argument("--success", required=True)
    target_set.add_argument("--artifact-kind", action="append", default=[])
    target_set.add_argument("--risk", action="append", default=[])

    environment_parser = subparsers.add_parser("environment", help="Connect a unit to a testing environment")
    environment_subparsers = environment_parser.add_subparsers(dest="environment_command", required=True)
    environment_connect = environment_subparsers.add_parser("connect", help="Create an environment contract")
    add_unit_reference(environment_connect)
    environment_connect.add_argument("--name", required=True)
    environment_connect.add_argument("--mode", choices=sorted(ENVIRONMENT_MODES), required=True)
    environment_connect.add_argument("--description", required=True)
    environment_connect.add_argument("--run-command")
    environment_connect.add_argument("--artifact-path")
    environment_connect.add_argument("--interaction-mode", choices=sorted(INTERACTION_MODES), default="command")
    environment_connect.add_argument("--tool", action="append", default=[])
    environment_connect.add_argument("--note", action="append", default=[])
    environment_status = environment_subparsers.add_parser("status", help="Print the environment contract")
    add_unit_reference(environment_status)

    attempt_parser = subparsers.add_parser("attempt", help="Plan and observe agent attempts")
    attempt_subparsers = attempt_parser.add_subparsers(dest="attempt_command", required=True)
    attempt_plan = attempt_subparsers.add_parser("plan", help="Create an agent-authored attempt plan")
    add_unit_reference(attempt_plan)
    attempt_plan.add_argument("--goal", required=True)
    attempt_plan.add_argument("--method", required=True)
    attempt_plan.add_argument("--action", action="append", default=[])
    attempt_plan.add_argument("--expected-artifact", action="append", default=[])
    attempt_plan.add_argument("--success-check", action="append", default=[])
    attempt_plan.add_argument("--note", action="append", default=[])

    attempt_observe = attempt_subparsers.add_parser("observe", help="Add an observation to an attempt")
    add_unit_reference(attempt_observe)
    attempt_observe.add_argument("attempt_id")
    attempt_observe.add_argument("--summary", required=True)
    attempt_observe.add_argument("--outcome", default="unknown")
    attempt_observe.add_argument("--run-id")
    attempt_observe.add_argument("--finding", action="append", default=[])

    attempt_conclude = attempt_subparsers.add_parser("conclude", help="Evaluate an attempt and choose its next lifecycle action")
    add_unit_reference(attempt_conclude)
    attempt_conclude.add_argument("attempt_id")
    attempt_conclude.add_argument("--run-id", required=True)
    attempt_conclude.add_argument("--outcome", required=True, choices=sorted(VALID_CONCLUSION_OUTCOMES))
    attempt_conclude.add_argument("--decision", required=True, choices=sorted(VALID_CONCLUSION_DECISIONS))
    attempt_conclude.add_argument("--summary", required=True)
    attempt_conclude.add_argument("--confidence", required=True, choices=sorted(VALID_CONFIDENCE))
    attempt_conclude.add_argument("--question")

    candidate_parser = subparsers.add_parser("candidate", help="Manage candidates")
    candidate_subparsers = candidate_parser.add_subparsers(dest="candidate_command", required=True)
    candidate_create = candidate_subparsers.add_parser("create", help="Create a candidate patch workspace")
    add_unit_reference(candidate_create)
    candidate_create.add_argument("--summary", required=True)
    candidate_create.add_argument("--kind", default="mixed", help="Legacy candidate classification")
    candidate_create.add_argument("--plane", choices=sorted(CANDIDATE_PLANES))
    candidate_create.add_argument("--validation-tier", choices=VALIDATION_TIERS, default="targeted")
    candidate_list = candidate_subparsers.add_parser("list", help="List candidate change batches")
    add_unit_reference(candidate_list)
    candidate_list.add_argument("--format", choices=["table", "json"], default="table")
    candidate_list.add_argument("--include-closed", action="store_true")
    candidate_show = candidate_subparsers.add_parser("show", help="Show one candidate record")
    add_unit_reference(candidate_show)
    candidate_show.add_argument("candidate_id")
    candidate_stage = candidate_subparsers.add_parser("stage", help="Change a candidate lifecycle stage")
    add_unit_reference(candidate_stage)
    candidate_stage.add_argument("candidate_id")
    candidate_stage.add_argument("status", choices=["accumulating", "ready", "validating", "rejected"])
    candidate_rebase = candidate_subparsers.add_parser("rebase", help="Rebase a stale parallel candidate")
    add_unit_reference(candidate_rebase)
    candidate_rebase.add_argument("candidate_id")
    candidate_evidence = candidate_subparsers.add_parser("evidence", help="Manage candidate evidence")
    candidate_evidence_subparsers = candidate_evidence.add_subparsers(dest="evidence_command", required=True)
    candidate_evidence_add = candidate_evidence_subparsers.add_parser("add", help="Add evidence to a candidate")
    add_unit_reference(candidate_evidence_add)
    candidate_evidence_add.add_argument("candidate_id")
    candidate_evidence_add.add_argument("--kind", required=True)
    candidate_evidence_add.add_argument("--summary", required=True)
    candidate_evidence_add.add_argument("--outcome", default="supports")
    candidate_evidence_add.add_argument("--run-id")
    candidate_evidence_add.add_argument("--artifact-id")
    candidate_evidence_add.add_argument("--path", type=Path)
    candidate_evidence_add.add_argument("--validation-tier", choices=VALIDATION_TIERS)

    promote_parser = subparsers.add_parser("promote", help="Promote a candidate into a version snapshot")
    add_unit_reference(promote_parser)
    promote_parser.add_argument("candidate_id")
    promote_parser.add_argument("--version", required=True)
    promote_parser.add_argument("--summary")
    promote_parser.add_argument("--allow-missing-evidence", action="store_true")

    rollback_parser = subparsers.add_parser("rollback", help="Restore a promoted version snapshot")
    add_unit_reference(rollback_parser)
    rollback_parser.add_argument("--to", required=True, dest="version")

    package_parser = subparsers.add_parser("package", help="Create a portable package from a promoted version")
    add_unit_reference(package_parser)
    package_parser.add_argument("--output", type=Path, required=True)
    package_parser.add_argument("--profile", default="thin")
    package_parser.add_argument("--version")

    export_parser = subparsers.add_parser("export", help="Export a harness unit for a target agent")
    add_unit_reference(export_parser)
    export_parser.add_argument("--adapter", required=True, choices=sorted(SUPPORTED_ADAPTERS))
    export_parser.add_argument("--output", type=Path)

    validate_parser = subparsers.add_parser("validate", help="Validate a harness unit")
    add_unit_reference(validate_parser)

    status_parser = subparsers.add_parser("status", help="Print harness unit lifecycle state")
    add_unit_reference(status_parser)
    status_parser.add_argument("--format", choices=["json", "markdown"], default="json")

    brief_parser = subparsers.add_parser("brief", help="Recover compact context for one harness unit")
    add_unit_reference(brief_parser)
    brief_parser.add_argument("--format", choices=["markdown", "json"], default="markdown")

    doctor_parser = subparsers.add_parser("doctor", help="Check local Harneloop runtime prerequisites")
    doctor_parser.add_argument("--json", action="store_true", dest="json_output")
    doctor_parser.add_argument("--cwd", type=Path, default=Path.cwd())

    run_parser = subparsers.add_parser("run", help="Manage runtime run records")
    run_subparsers = run_parser.add_subparsers(dest="run_command", required=True)

    run_start = run_subparsers.add_parser("start", help="Start a run record")
    add_unit_reference(run_start)
    run_start.add_argument("--task", required=True)
    run_start.add_argument("--candidate-id")
    run_start.add_argument("--attempt-id")
    run_start.add_argument("--benchmark-id")
    run_start.add_argument("--benchmark-task-id")
    run_start.add_argument("--benchmark-trial", type=int)
    run_start.add_argument("--benchmark-arm", choices=["baseline", "candidate"])

    run_finish = run_subparsers.add_parser("finish", help="Finish a run record")
    add_unit_reference(run_finish)
    run_finish.add_argument("run_id")
    run_finish.add_argument("--status", required=True, choices=["succeeded", "failed", "stopped"])
    run_finish.add_argument("--summary")

    artifact_parser = subparsers.add_parser("artifact", help="Manage run artifacts")
    artifact_subparsers = artifact_parser.add_subparsers(dest="artifact_command", required=True)
    artifact_add = artifact_subparsers.add_parser("add", help="Attach an artifact to a run")
    add_unit_reference(artifact_add)
    artifact_add.add_argument("run_id")
    artifact_add.add_argument("source", type=Path)
    artifact_add.add_argument("--kind", required=True)
    artifact_add.add_argument("--description", default="")
    artifact_add.add_argument("--name")

    state_parser = subparsers.add_parser("state", help="Manage wait, stop, and resume states")
    state_subparsers = state_parser.add_subparsers(dest="state_command", required=True)

    wait_parser = state_subparsers.add_parser("wait", help="Mark a unit as waiting")
    add_unit_reference(wait_parser)
    wait_parser.add_argument("--reason", required=True)
    wait_parser.add_argument("--next-action", required=True)
    wait_parser.add_argument("--resume-after")
    wait_parser.add_argument("--resume-condition")

    stop_parser = state_subparsers.add_parser("stop", help="Mark a unit as stopped")
    add_unit_reference(stop_parser)
    stop_parser.add_argument("--reason", required=True)
    stop_parser.add_argument("--next-action")

    resume_parser = state_subparsers.add_parser("resume", help="Return a unit to active state")
    add_unit_reference(resume_parser)
    resume_parser.add_argument("--reason", default="manual_resume")
    resume_parser.add_argument("--next-action")

    return parser


def coerce_cli_value(value: str) -> object:
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return value


def read_json_object(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise HarneloopError(f"Cannot read JSON input: {error}") from error
    if not isinstance(data, dict):
        raise HarneloopError("JSON input must be an object")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command is None:
            if not sys.stdin.isatty():
                parser.print_help()
                return 2
            from .interactive import run_interactive_menu

            return run_interactive_menu()

        if hasattr(args, "unit") and args.command != "units":
            args.unit = resolve_unit_reference(args.unit)

        if args.command == "benchmark":
            if args.benchmark_command == "create":
                data = create_benchmark(args.unit, args.benchmark_id, read_json_object(args.manifest))
            elif args.benchmark_command == "show":
                data = read_benchmark(args.unit, args.benchmark_id)
            elif args.benchmark_command == "bind":
                from .benchmarks import bind_benchmark_candidate
                data = bind_benchmark_candidate(args.unit, args.benchmark_id, args.candidate_id)
            elif args.benchmark_command == "compare":
                data = compare_benchmark(args.unit, args.benchmark_id, candidate_id=args.candidate_id)
            else:
                row = read_json_object(args.result)
                required = {"observed_conditions", "arm", "task_id", "trial", "run_id", "score", "artifact_ids"}
                allowed = required | {"status", "reason", "metrics"}
                if set(row) - allowed or required - set(row):
                    raise HarneloopError("Benchmark result has unknown fields or is missing required fields")
                data = record_benchmark_result(args.unit, args.benchmark_id, **row)
            print(json.dumps(data, indent=2))
            return 0

        if args.command == "refine":
            if args.refine_command == "compose":
                candidate = compose_candidates(args.unit, args.candidate_id, args.summary)
                data = read_candidate(args.unit, candidate.name)
            elif args.refine_command == "learn":
                data = add_lesson(args.unit, args.cue, args.lesson, args.run_id, args.artifact_id)
            else:
                data = list_lessons(args.unit, args.query)
            print(json.dumps(data, indent=2))
            return 0

        if args.command == "onboard":
            if args.format == "json":
                print(json.dumps(render_onboarding_json(), indent=2))
            else:
                print(render_onboarding_markdown(), end="")
            return 0

        if args.command == "intake":
            if args.intake_command == "status":
                data = read_intake(args.unit)
                print(json.dumps(data, indent=2) if args.format == "json" else render_intake_markdown(data), end="" if args.format == "markdown" else "\n")
                return 0
            if args.intake_command == "resolve":
                data = resolve_intake_field(
                    args.unit,
                    args.field,
                    value=args.value,
                    status=args.status,
                    source=args.source,
                )
                print(json.dumps(data["fields"][args.field], indent=2))
                return 0
            if args.intake_command == "acknowledge":
                data = acknowledge_intake(args.unit, basis=args.basis, note=args.note)
                print(json.dumps(data["acknowledgement"], indent=2))
                return 0

        if args.command == "setup":
            if not sys.stdin.isatty():
                print("error: `harneloop setup` requires an interactive terminal", file=sys.stderr)
                return 2
            from .interactive import run_interactive_setup

            return run_interactive_setup(args.home)

        if args.command == "settings":
            if args.settings_command == "show":
                print(json.dumps(load_preferences(args.home), indent=2))
                return 0
            if args.settings_command == "set":
                updated = update_preference(args.home, args.key, coerce_cli_value(args.value))
                print(json.dumps(updated, indent=2))
                return 0

        if args.command == "units":
            if args.units_command == "list":
                units = list_registered_units(args.home)
                if not units:
                    print("No registered harness units.")
                    return 0
                for unit in units:
                    print(f"{unit.get('id')}\t{unit.get('name')}\t{unit.get('path')}")
                return 0
            if args.units_command == "register":
                if not (args.unit / "unit.yaml").exists():
                    raise HarneloopError(f"Not a Harneloop harness unit: {args.unit}")
                record = register_unit(args.home, args.unit)
                print(f"Registered harness unit: {record['name']} ({record['id']})")
                return 0
            if args.units_command == "remove":
                removed = remove_registered_unit(args.home, args.unit_id_or_path)
                print("Removed harness unit registry entry." if removed else "No matching harness unit found.")
                return 0

        if args.command == "init-unit":
            path = init_unit(args.path, args.id, args.name, args.template)
            print(f"Created harness unit: {path}")
            if not args.no_register:
                record = register_unit(args.home, path)
                print(f"Registered harness unit: {record['name']} ({record['id']})")
            return 0

        if args.command == "upgrade-unit":
            created = upgrade_unit_protocol(args.unit)
            if created:
                print("Added protocol files:")
                for path in created:
                    print(f"- {path}")
            else:
                print("Harness unit already uses the current protocol.")
            return 0

        if args.command == "template" and args.template_command == "list":
            for template in list_templates():
                print(template)
            return 0

        if args.command == "target" and args.target_command == "set":
            brief = set_target_brief(
                args.unit,
                task=args.task,
                success=args.success,
                artifact_kind=args.artifact_kind,
                risk=args.risk,
            )
            print(json.dumps(brief, indent=2))
            return 0

        if args.command == "environment":
            if args.environment_command == "connect":
                contract = connect_environment(
                    args.unit,
                    name=args.name,
                    mode=args.mode,
                    description=args.description,
                    run_command=args.run_command,
                    artifact_path=args.artifact_path,
                    interaction_mode=args.interaction_mode,
                    tool=args.tool,
                    notes=args.note,
                )
                print(json.dumps(contract, indent=2))
                return 0
            if args.environment_command == "status":
                print(render_environment_status(args.unit), end="")
                return 0

        if args.command == "candidate":
            if args.candidate_command == "create":
                path = create_candidate(
                    args.unit,
                    args.summary,
                    args.kind,
                    plane=args.plane,
                    validation_tier=args.validation_tier,
                )
                print(f"Created candidate: {path.name}")
                return 0
            if args.candidate_command == "list":
                records = list_candidates(args.unit, include_closed=args.include_closed)
                if args.format == "json":
                    print(json.dumps(records, indent=2))
                elif not records:
                    print("No matching candidates.")
                else:
                    for record in records:
                        print(
                            f"{record.get('id')}\t{record.get('status')}\t{record.get('plane', record.get('kind'))}\t"
                            f"{record.get('validation_tier', 'legacy')}\t{record.get('summary')}"
                        )
                return 0
            if args.candidate_command == "show":
                print(json.dumps(read_candidate(args.unit, args.candidate_id), indent=2))
                return 0
            if args.candidate_command == "stage":
                record = set_candidate_status(args.unit, args.candidate_id, args.status)
                print(json.dumps(record, indent=2))
                return 0
            if args.candidate_command == "rebase":
                record = rebase_candidate(args.unit, args.candidate_id)
                print(json.dumps(record, indent=2))
                return 0

        if args.command == "attempt":
            if args.attempt_command == "plan":
                attempt = create_attempt_plan(
                    args.unit,
                    goal=args.goal,
                    method=args.method,
                    action=args.action,
                    expected_artifact=args.expected_artifact,
                    success_check=args.success_check,
                    note=args.note,
                )
                print(json.dumps(attempt, indent=2))
                return 0
            if args.attempt_command == "observe":
                observation = add_attempt_observation(
                    args.unit,
                    args.attempt_id,
                    summary=args.summary,
                    outcome=args.outcome,
                    run_id=args.run_id,
                    finding=args.finding,
                )
                print(json.dumps(observation, indent=2))
                return 0
            if args.attempt_command == "conclude":
                conclusion = conclude_attempt(
                    args.unit,
                    args.attempt_id,
                    run_id=args.run_id,
                    outcome=args.outcome,
                    decision=args.decision,
                    summary=args.summary,
                    confidence=args.confidence,
                    question=args.question,
                )
                print(json.dumps(conclusion, indent=2))
                return 0

        if args.command == "candidate" and args.candidate_command == "evidence":
            if args.evidence_command == "add":
                record = add_evidence(
                    args.unit,
                    args.candidate_id,
                    kind=args.kind,
                    summary=args.summary,
                    outcome=args.outcome,
                    run_id=args.run_id,
                    artifact_id=args.artifact_id,
                    path=args.path,
                    validation_tier=args.validation_tier,
                )
                print(json.dumps(record, indent=2))
                return 0

        if args.command == "promote":
            path = promote_candidate(
                args.unit,
                args.candidate_id,
                args.version,
                args.summary,
                require_evidence=not args.allow_missing_evidence,
            )
            print(f"Promoted {args.candidate_id} to {path.name}")
            return 0

        if args.command == "rollback":
            path = rollback_unit(args.unit, args.version)
            print(f"Rolled back to {path.name}")
            return 0

        if args.command == "package":
            output = package_unit(args.unit, args.output, args.profile, args.version)
            print(f"Created package: {output}")
            return 0

        if args.command == "export":
            output = export_unit(args.unit, args.adapter, args.output)
            print(f"Created {args.adapter} export: {output}")
            return 0

        if args.command == "validate":
            issues = validate_unit(args.unit)
            if issues:
                for issue in issues:
                    print(f"{issue.path}: {issue.message}", file=sys.stderr)
                return 1
            print("Unit is valid")
            return 0

        if args.command == "status":
            state = read_state(args.unit)
            if args.format == "markdown":
                print(render_state_markdown(state), end="")
            else:
                print(json.dumps(state, indent=2))
            return 0

        if args.command == "brief":
            data = build_session_brief_data(args.unit)
            if args.format == "json":
                print(json.dumps(data, indent=2))
            else:
                print(render_session_brief_markdown(data), end="")
            return 0

        if args.command == "doctor":
            checks = run_doctor(args.cwd)
            if args.json_output:
                print(json.dumps([check.to_dict() for check in checks], indent=2))
            else:
                for check in checks:
                    label = "OK" if check.ok else "FAIL"
                    print(f"{label} {check.name}: {check.detail}")
            return 0 if all(check.ok for check in checks) else 1

        if args.command == "run":
            if args.run_command == "start":
                benchmark_fields = {}
                if any(value is not None for value in [args.benchmark_id, args.benchmark_task_id, args.benchmark_trial, args.benchmark_arm]):
                    benchmark_fields = {"benchmark_id": args.benchmark_id, "benchmark_task_id": args.benchmark_task_id,
                                        "benchmark_trial": args.benchmark_trial, "benchmark_arm": args.benchmark_arm}
                path = start_run(args.unit, args.task, args.candidate_id, args.attempt_id, **benchmark_fields)
                print(f"Started run: {path.name}")
                return 0
            if args.run_command == "finish":
                record = finish_run(args.unit, args.run_id, args.status, args.summary)
                print(json.dumps(record, indent=2))
                return 0

        if args.command == "artifact" and args.artifact_command == "add":
            record = add_artifact(
                args.unit,
                args.run_id,
                args.source,
                kind=args.kind,
                description=args.description,
                name=args.name,
            )
            print(json.dumps(record, indent=2))
            return 0

        if args.command == "state":
            if args.state_command == "wait":
                state = mark_waiting(
                    args.unit,
                    args.reason,
                    args.next_action,
                    resume_after=args.resume_after,
                    resume_condition=args.resume_condition,
                )
            elif args.state_command == "stop":
                state = mark_stopped(args.unit, args.reason, args.next_action)
            elif args.state_command == "resume":
                state = mark_active(args.unit, args.reason, args.next_action)
            else:
                parser.error("Unknown state command")
            print(json.dumps(state, indent=2))
            return 0

    except HarneloopError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    parser.error("Unhandled command")
    return 2
