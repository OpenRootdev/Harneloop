import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from harneloop.cli import main
from harneloop.unit import init_unit


class BenchmarkCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.unit = init_unit(self.root / "unit", "cli-test", "CLI Test")
        self.manifest = {
            "source": {"name": "external-benchmark", "revision": "a" * 40},
            "tasks": [{"task_id": "dev-1", "split": "dev"}, {"task_id": "test-1", "split": "test"}],
            "repeats": 1, "seed": 42, "executor": "external-agent", "model": "fixed-model",
            "effort": "high", "settings": {}, "tools": {"terminal": True},
            "budget_policy": {"calls": 10}, "grader": {"name": "deterministic", "revision": "b" * 40},
        }

    def call(self, args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = main(args)
        return status, output.getvalue(), errors.getvalue()

    def test_create_and_show_frozen_manifest(self):
        path = self.root / "manifest.json"
        path.write_text(json.dumps(self.manifest), encoding="utf-8")
        status, output, _ = self.call(["benchmark", "create", str(self.unit), "pilot", "--manifest", str(path)])
        self.assertEqual(status, 0)
        digest = json.loads(output)["manifest_sha256"]
        status, output, _ = self.call(["benchmark", "show", str(self.unit), "pilot"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output)["manifest_sha256"], digest)

    def test_invalid_json_returns_domain_error_without_traceback(self):
        path = self.root / "bad.json"
        path.write_text("{bad", encoding="utf-8")
        status, _, error = self.call(["benchmark", "create", str(self.unit), "pilot", "--manifest", str(path)])
        self.assertEqual(status, 2)
        self.assertIn("error:", error)
        self.assertNotIn("Traceback", error)

    def test_record_rejects_unexpected_fields(self):
        path = self.root / "bad-row.json"
        path.write_text('{"unit_root":"escape"}', encoding="utf-8")
        status, _, error = self.call(["benchmark", "record", str(self.unit), "pilot", "--result", str(path)])
        self.assertEqual(status, 2)
        self.assertIn("error:", error)


if __name__ == "__main__":
    unittest.main()
