"""Render terminal UI scenarios without launching an orchestration run.

Examples::

    python -m genesis.ui._preview --width 88 --height 24
    python -m genesis.ui._preview --scenario blocked --width 140 --height 30
    python -m genesis.ui._preview --scenario all --width 110 --height 26
"""
from __future__ import annotations

import argparse

from rich.console import Console

from genesis.schemas.plan import Plan, Step
from genesis.ui.dashboard import DashboardState, DashboardView, UsageStats


SCENARIOS = ("idle", "running", "parallel", "blocked", "complete")


def _mock_plan() -> Plan:
    return Plan(
        task_id="run-a7f2",
        task_summary="Add OAuth login flow with tests",
        estimated_steps=5,
        steps=[
            Step(
                step_id="s1",
                title="Scaffold auth module",
                description="...",
                type="code",
                preferred_agent="codex-main",
                file_scope=["genesis/auth/__init__.py", "genesis/auth/oauth.py"],
                expected_output="module",
            ),
            Step(
                step_id="s2",
                title="Implement token exchange",
                description="...",
                type="code",
                preferred_agent="codex-main",
                depends_on=["s1"],
                file_scope=["genesis/auth/oauth.py"],
                expected_output="code",
            ),
            Step(
                step_id="s3",
                title="Wire login command into REPL",
                description="...",
                type="code",
                preferred_agent="claude-cli-worker",
                depends_on=["s2"],
                file_scope=["genesis/repl.py"],
                expected_output="code",
            ),
            Step(
                step_id="s4",
                title="Write unit tests for token exchange",
                description="...",
                type="test",
                preferred_agent="codex-main",
                depends_on=["s2"],
                file_scope=["tests/test_oauth.py"],
                expected_output="tests",
            ),
            Step(
                step_id="s5",
                title="Document the auth flow",
                description="...",
                type="docs",
                preferred_agent="claude-cli-worker",
                depends_on=["s3", "s4"],
                file_scope=["README.md"],
                expected_output="docs",
            ),
        ],
    )


def _mock_state(scenario: str = "running") -> DashboardState:
    plan = _mock_plan()
    state = DashboardState(
        agent_names=[
            "claude-cli-orchestrator",
            "claude-cli-worker",
            "codex-main",
            "codex-reserve",
        ]
    )
    state.task_name = "Add OAuth login flow with tests"
    state.plan = plan
    state.total = len(plan.steps)
    state.git_sha = "a7b5086"
    state.chat_url = "http://127.0.0.1:8765"

    for step in plan.steps:
        state.step_scopes[step.step_id] = ", ".join(step.file_scope)
        state.step_statuses[step.step_id] = "pending"

    if scenario == "idle":
        state.run_phase = "planning"
        state.add_event("WAIT", "planner is negotiating scope", "dim")
        state.add_output("Preparing repository context...")
        return state

    state.completed = 2
    state.run_phase = "running"
    state.current_step = "s3"
    state.current_worker = "claude-cli-worker"
    state.active_steps["s3"] = "claude-cli-worker"
    state.step_statuses.update(
        {"s1": "committed", "s2": "committed", "s3": "running"}
    )
    state.step_workers.update(
        {"s1": "codex-main", "s2": "codex-main", "s3": "claude-cli-worker"}
    )
    state.step_reviewers.update(
        {"s1": "independent-reviewer", "s2": "independent-reviewer"}
    )
    state.step_verification.update({"s1": "passed", "s2": "passed"})
    state.step_repairs["s2"] = 1
    state.step_elapsed.update({"s1": 42.0, "s2": 88.0})

    if scenario == "parallel":
        state.step_statuses["s4"] = "running"
        state.step_workers["s4"] = "codex-main"
        state.active_steps["s4"] = "codex-main"

    for label, detail, style in [
        ("PLAN", "5 steps locked", "cyan"),
        ("LEASE", "codex-main -> s1", "green"),
        ("COMMIT", "s1: 3f2a1b", "green"),
        ("REVIEW", "s2: approved 9/10", "green"),
        ("COMMIT", "s2: a7b5086", "green"),
        ("START", "s3 Wire login command into REPL", "cyan"),
        ("LEASE", "claude-cli-worker -> s3", "green"),
    ]:
        state.add_event(label, detail, style)

    for line in [
        "[cyan]STEP[/cyan] [bold]s3[/bold] Wire login command into REPL",
        "  [dim]worker[/dim] [cyan]claude-cli-worker[/cyan]",
        "  Reading genesis/repl.py ...",
        "  Adding `login` command handler to _run_loop",
        "  verify $ python -m pytest tests/test_ui_rendering.py -q",
        "  Tokens: in=18432 out=2104 | $0.0461",
    ]:
        state.add_output(line, trusted_markup=True)
    state.record_token_line("Tokens: in=18432 out=2104 | $0.0461")
    orchestrator_usage = UsageStats()
    orchestrator_usage.absorb_line("Tokens: in=20110 out=1978 | $0.0510")
    state.usage["__orch__"] = orchestrator_usage

    if scenario == "parallel":
        state.add_event("LEASE", "codex-main -> s4", "green")
        state.add_output("  codex-main/s4 | collecting regression fixtures")
    elif scenario == "blocked":
        state.run_phase = "blocked"
        state.step_statuses["s3"] = "blocked"
        state.blocked_reason = "verification failed: 2 UI snapshot assertions"
        state.add_event("BLOCK", state.blocked_reason, "red")
        state.add_output("  x verification failed with exit 1")
    elif scenario == "complete":
        state.run_phase = "completed"
        state.completed = state.total
        state.current_step = ""
        state.current_worker = ""
        state.active_steps.clear()
        for step in plan.steps:
            state.step_statuses[step.step_id] = "committed"
            state.step_verification[step.step_id] = "passed"
        state.add_event("RELEASE", "all quality locks passed", "green")
        state.add_output("+ Task complete  5/5 steps", trusted_markup=False)

    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview responsive Genesis terminal UI states.")
    parser.add_argument("--width", type=int, default=None, help="render width (defaults to terminal width)")
    parser.add_argument("--height", type=int, default=26, help="render height (default: 26)")
    parser.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="running")
    args = parser.parse_args()

    console = Console(width=args.width, height=args.height)
    scenarios = SCENARIOS if args.scenario == "all" else (args.scenario,)
    for index, scenario in enumerate(scenarios):
        if index:
            console.print()
        console.rule(f"[bold]TERMINAL / {scenario.upper()} / {console.width}x{args.height}[/bold]")
        console.print(DashboardView(_mock_state(scenario)), height=args.height)


if __name__ == "__main__":
    main()
