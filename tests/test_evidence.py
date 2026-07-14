import types
import unittest

from genesis.evidence import evaluate_patch_evidence
from genesis.worktree import WorktreePatch


def _step(scope):
    return types.SimpleNamespace(file_scope=scope)


def _patch(files, statuses):
    return WorktreePatch(
        worktree_path=".",
        patch_text="patch",
        changed_files=files,
        diff_status_lines=statuses,
    )


class EvidenceGuardTests(unittest.TestCase):
    def test_rejects_cache_artifacts(self):
        result = evaluate_patch_evidence(
            _step([]),
            _patch(
                ["app.py", ".pytest_cache/v/cache/nodeids"],
                ["M\tapp.py", "A\t.pytest_cache/v/cache/nodeids"],
            ),
        )

        self.assertFalse(result.passed)
        self.assertEqual(
            [".pytest_cache/v/cache/nodeids"],
            result.artifact_files,
        )

    def test_rejects_deletion_outside_declared_scope(self):
        result = evaluate_patch_evidence(
            _step(["config/**", ".env.example"]),
            _patch(["brain.html"], ["D\tbrain.html"]),
        )

        self.assertFalse(result.passed)
        self.assertEqual(["brain.html"], result.out_of_scope_deletions)

    def test_allows_deletion_inside_declared_scope(self):
        result = evaluate_patch_evidence(
            _step(["credentials/**"]),
            _patch(
                ["credentials/old.json"],
                ["D\tcredentials/old.json"],
            ),
        )

        self.assertTrue(result.passed)


if __name__ == "__main__":
    unittest.main()
