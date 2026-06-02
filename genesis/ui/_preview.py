"""
UI preview harness — renders the static + live surfaces with mock data so the
terminal UI can be eyeballed without launching a real orchestration run.

    python -m genesis.ui._preview            # full-color, your terminal width
    python -m genesis.ui._preview --width 120

Useful during UI work: tweak a panel, re-run, see the result instantly.
"""
from __future__ import annotations

import argparse

from rich.console import Console

from genesis.schemas.plan import Plan, Step
from genesis.ui.dashboard import DashboardState, make_layout


def _mock_plan() -> Plan:
    return Plan(
        task_id="demo",
        task_summary="Add OAuth login flow with tests",
        estimated_steps=5,
        steps=[
            Step(step_id="s1", title="Scaffold auth module", description="...",
                 type="code", preferred_agent="codex-main",
                 file_scope=["genesis/auth/__init__.py", "genesis/auth/oauth.py"],
                 expected_output="module"),
            Step(step_id="s2", title="Implement token exchange", description="...",
                 type="code", preferred_agent="codex-main", depends_on=["s1"],
                 file_scope=["genesis/auth/oauth.py"], expected_output="code"),
            Step(step_id="s3", title="Wire login command into REPL", description="...",
                 type="code", preferred_agent="claude-cli-worker", depends_on=["s2"],
                 file_scope=["genesis/repl.py"], expected_output="code"),
            Step(step_id="s4", title="Write unit tests for token exchange", description="...",
                 type="test", preferred_agent="codex-main", depends_on=["s2"],
                 file_scope=["tests/test_oauth.py"], expected_output="tests"),
            Step(step_id="s5", title="Document the auth flow", description="...",
                 type="docs", preferred_agent="claude-cli-worker", depends_on=["s3", "s4"],
                 file_scope=["README.md"], expected_output="docs"),
        ],
    )


def _mock_state() -> DashboardState:
    plan = _mock_plan()
    state = DashboardState(agent_names=[
        "claude-cli-orchestrator", "claude-cli-worker", "codex-main", "codex-pro2",
    ])
    state.task_name = "Add OAuth login flow with tests"
    state.plan = plan
    state.total = len(plan.steps)
    state.completed = 2
    state.run_phase = "running"
    state.current_step = "s3"
    state.current_worker = "claude-cli-worker"
    state.git_sha = "a7b5086"

    for s in plan.steps:
        state.step_scopes[s.step_id] = ", ".join(s.file_scope)
    state.step_statuses = {
        "s1": "committed", "s2": "committed", "s3": "running",
        "s4": "pending", "s5": "pending",
    }
    state.step_workers = {"s1": "codex-main", "s2": "codex-main", "s3": "claude-cli-worker"}
    state.step_reviewers = {"s1": "independent-reviewer", "s2": "independent-reviewer"}
    state.step_verification = {"s1": "passed", "s2": "passed"}
    state.step_repairs = {"s2": 1}
    state.step_elapsed = {"s1": 42.0, "s2": 88.0}

    for label, detail, style in [
        ("PLAN", "5 steps", "cyan"),
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
        "  Tokens: in=18432 out=2104 | $0.0461",
    ]:
        state.add_output(line, trusted_markup=True)
    state.record_token_line("Tokens: in=18432 out=2104 | $0.0461")
    state.usage["__orch__"] = state.usage.get("__orch__") or state.usage[next(iter(state.usage))]
    return state


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--width", type=int, default=None)
    args = parser.parse_args()

    console = Console(width=args.width)
    state = _mock_state()

    console.rule("[bold]LIVE DASHBOARD (mid-run snapshot)[/bold]")
    console.print(make_layout(state), height=24)
    console.print()


if __name__ == "__main__":
    main()
