import tempfile
import unittest
import os
import stat
from pathlib import Path

from harneloop.candidate import create_candidate, read_candidate
from harneloop.errors import HarneloopError
from harneloop.evidence import list_evidence
from harneloop.intake import acknowledge_intake
from harneloop.runs import start_run, add_artifact, finish_run
from harneloop.unit import init_unit
from harneloop.refinement import compose_candidates, add_lesson, list_lessons
from harneloop.yamlio import write_yaml


class RefinementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.unit = init_unit(Path(self.temp.name) / "unit", "refine", "Refinement")
        acknowledge_intake(self.unit, basis="user_delegated", note="Test setup delegated")

    def candidate(self, name, text):
        c = create_candidate(self.unit, name, plane="target_harness", validation_tier="representative")
        p = c / "changes/agent-facing" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return c

    def test_composition_preserves_sources_and_requires_fresh_evidence(self):
        a, b = self.candidate("a.md", "A"), self.candidate("b.md", "B")
        result = compose_candidates(self.unit, [a.name, b.name], "Combine orthogonal changes")
        self.assertEqual((result / "changes/agent-facing/a.md").read_text(), "A")
        self.assertEqual((result / "changes/agent-facing/b.md").read_text(), "B")
        self.assertEqual(read_candidate(self.unit, result.name)["validation_tier"], "representative")
        self.assertEqual(list_evidence(self.unit, result.name), [])
        self.assertEqual(read_candidate(self.unit, a.name)["status"], "accumulating")

    def test_conflicting_composition_is_rejected_without_creating_candidate(self):
        a, b = self.candidate("same.md", "A"), self.candidate("same.md", "B")
        before = sorted((self.unit / "candidates").iterdir())
        with self.assertRaises(HarneloopError):
            compose_candidates(self.unit, [a.name, b.name], "Conflict")
        self.assertEqual(before, sorted((self.unit / "candidates").iterdir()))

    def test_protected_source_files_are_rejected(self):
        a, b = self.candidate("a.md", "A"), self.candidate("b.md", "B")
        (a / "changes/unit.yaml").write_text("bad: true")
        with self.assertRaises(HarneloopError):
            compose_candidates(self.unit, [a.name, b.name], "Invalid")

    def test_cross_plane_composition_is_rejected(self):
        a = self.candidate("a.md", "A")
        b = create_candidate(self.unit, "Grader change", plane="evaluation")
        with self.assertRaises(HarneloopError):
            compose_candidates(self.unit, [a.name, b.name], "Mixed evidence")

    @unittest.skipIf(os.name == 'nt', 'Windows does not expose POSIX executable mode')
    def test_composition_preserves_executable_mode(self):
        a, b = self.candidate('script.sh', '#!/bin/sh\nexit 0\n'), self.candidate('b.md', 'B')
        (a / 'changes/agent-facing/script.sh').chmod(0o755)
        result = compose_candidates(self.unit, [a.name, b.name], 'Keep runnable script')
        self.assertEqual(stat.S_IMODE((result / 'changes/agent-facing/script.sh').stat().st_mode), 0o755)

    def test_malformed_lesson_produces_actionable_error(self):
        root = self.unit / '.evolve/lessons'
        root.mkdir(parents=True, exist_ok=True)
        write_yaml(root / 'lesson-0001.yaml', {'evidence': None})
        with self.assertRaises(HarneloopError):
            list_lessons(self.unit)

    def test_lesson_requires_finished_run_and_rechecks_evidence(self):
        run = start_run(self.unit, "Observed edge case")
        p = Path(self.temp.name) / "result.txt"
        p.write_text("Concrete observation")
        artifact = add_artifact(self.unit, run.name, p, "test-result")
        with self.assertRaises(HarneloopError):
            add_lesson(self.unit, "boundary inputs", "Test both sides of each boundary", run.name, artifact["id"])
        finish_run(self.unit, run.name, "succeeded", "Observed result")
        lesson = add_lesson(self.unit, "boundary inputs", "Test both sides of each boundary", run.name, artifact["id"])
        self.assertEqual(list_lessons(self.unit, "boundary")[0]["id"], lesson["id"])
        self.assertEqual(list_lessons(self.unit, "unrelated"), [])
        (self.unit / artifact["stored_path"]).write_text("Changed after observation")
        with self.assertRaises(HarneloopError):
            list_lessons(self.unit)


if __name__ == "__main__":
    unittest.main()
