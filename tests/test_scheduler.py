from __future__ import annotations

import unittest

from genesis.agents.orchestrator import _topo_sort
from genesis.scheduler import (
    DependencyScheduler,
    StepScope,
    declared_step_scope,
    effective_step_scope,
    infer_step_scope,
    scopes_overlap,
)
from genesis.schemas.plan import Plan, Step


def make_step(
    step_id: str,
    *,
    depends_on: list[str] | None = None,
    context_hint: str = "",
    description: str = "Update a concrete file.",
    file_scope: list[str] | None = None,
) -> Step:
    return Step(
        step_id=step_id,
        title=f"Step {step_id}",
        description=description,
        type="code",
        preferred_agent="any",
        depends_on=depends_on or [],
        file_scope=file_scope or [],
        expected_output="Done.",
        context_hint=context_hint,
    )


class SchedulerTests(unittest.TestCase):
    def test_infers_concrete_paths(self) -> None:
        scope = infer_step_scope(make_step("step-1", context_hint="genesis/scheduler.py"))

        self.assertEqual(("genesis/scheduler.py",), scope.paths)
        self.assertEqual("inferred", scope.source)

    def test_old_plans_without_file_scope_still_load(self) -> None:
        plan = Plan(
            task_id="run-old",
            task_summary="old checkpoint",
            estimated_steps=1,
            steps=[
                {
                    "step_id": "step-1",
                    "title": "Legacy",
                    "description": "Update src/app.py.",
                    "type": "code",
                    "preferred_agent": "any",
                    "depends_on": [],
                    "expected_output": "Done.",
                    "context_hint": "src/app.py",
                }
            ],
        )

        self.assertEqual([], plan.steps[0].file_scope)

    def test_unknown_or_broad_work_uses_wildcard_scope(self) -> None:
        broad = infer_step_scope(make_step("step-1", description="Update project architecture."))
        unknown = infer_step_scope(make_step("step-2", context_hint="", description="Improve behavior."))

        self.assertEqual(("*",), broad.paths)
        self.assertEqual(("*",), unknown.paths)

    def test_scope_overlap_handles_prefixes_and_wildcards(self) -> None:
        self.assertTrue(scopes_overlap(StepScope("a", ("src",)), StepScope("b", ("src/app.py",))))
        self.assertTrue(scopes_overlap(StepScope("a", ("*",)), StepScope("b", ("docs/readme.md",))))
        self.assertFalse(scopes_overlap(StepScope("a", ("src/app.py",)), StepScope("b", ("docs/readme.md",))))

    def test_declared_scope_wins_over_inferred_scope(self) -> None:
        step = make_step(
            "step-1",
            context_hint="src/inferred.py",
            file_scope=["docs/declared.md"],
        )

        self.assertEqual(("docs/declared.md",), declared_step_scope(step).paths)
        scope = effective_step_scope(step)
        self.assertEqual(("docs/declared.md",), scope.paths)
        self.assertEqual("declared", scope.source)

    def test_declared_broad_or_invalid_scope_serializes(self) -> None:
        broad = effective_step_scope(make_step("step-1", file_scope=["package.json"]))
        invalid = effective_step_scope(make_step("step-2", file_scope=["../outside.py"]))

        self.assertEqual(("*",), broad.paths)
        self.assertEqual(("*",), invalid.paths)

    def test_declared_glob_scopes_serialize_conservatively(self) -> None:
        for index, path in enumerate([
            "src/**/*.py",
            "src/file?.py",
            "src/[ab].py",
            "src/{app,cli}.py",
        ]):
            with self.subTest(path=path):
                scope = effective_step_scope(
                    make_step(f"step-{index}", file_scope=[path])
                )
                self.assertEqual(("*",), scope.paths)
                self.assertEqual("declared", scope.source)

    def test_selects_only_ready_non_overlapping_steps(self) -> None:
        scheduler = DependencyScheduler([
            make_step("step-1", context_hint="src/a.py"),
            make_step("step-2", context_hint="src/a.py", file_scope=["docs/b.md"]),
            make_step("step-3", depends_on=["step-1"], context_hint="src/c.py"),
            make_step("step-4", context_hint="src/a.py"),
        ])

        selected = scheduler.select_ready(
            committed_ids=set(),
            unavailable_ids=set(),
            active_scopes=[],
            limit=4,
        )

        self.assertEqual(["step-1", "step-2"], [item.step.step_id for item in selected])

    def test_topological_sort_rejects_duplicate_step_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "Duplicate step ID"):
            _topo_sort([make_step("duplicate"), make_step("duplicate")])

    def test_topological_sort_rejects_empty_step_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty step ID"):
            _topo_sort([make_step("   ")])

    def test_topological_sort_rejects_unknown_dependencies(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown step dependencies.*missing"):
            _topo_sort([make_step("step-1", depends_on=["missing"])])


if __name__ == "__main__":
    unittest.main()
