import tempfile
import unittest
from pathlib import Path

from harneloop.candidate import create_candidate, set_candidate_status
from harneloop.errors import HarneloopError
from harneloop.evidence import add_evidence
from harneloop.intake import acknowledge_intake
from harneloop.runs import add_artifact, finish_run, start_run
from harneloop.unit import init_unit
from harneloop.versioning import promote_candidate


class EvidenceIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.unit = init_unit(Path(self.temp.name) / 'unit', 'integrity', 'Integrity')
        acknowledge_intake(self.unit, basis='user_delegated', note='Test setup')
        self.candidate = create_candidate(self.unit, 'Improve policy')
        self.policy = self.candidate / 'changes/agent-facing/policy.md'
        self.policy.parent.mkdir(parents=True, exist_ok=True)
        self.policy.write_text('Tested policy')
        self.run = start_run(self.unit, 'Check policy', candidate_id=self.candidate.name)
        result = Path(self.temp.name) / 'result.txt'
        result.write_text('Observed result')
        self.artifact = add_artifact(self.unit, self.run.name, result, 'test-result')
        finish_run(self.unit, self.run.name, 'succeeded', 'Observed result')

    def attach(self):
        return add_evidence(self.unit, self.candidate.name, 'test', 'Observed result',
                            run_id=self.run.name, artifact_id=self.artifact['id'])

    def test_edited_overlay_cannot_attach_old_candidate_run(self):
        self.policy.write_text('Untested policy')
        with self.assertRaisesRegex(HarneloopError, 'changed'):
            self.attach()

    def test_promotion_rechecks_overlay_after_evidence_attachment(self):
        self.attach()
        set_candidate_status(self.unit, self.candidate.name, 'ready')
        self.policy.write_text('Untested policy')
        with self.assertRaisesRegex(HarneloopError, 'changed'):
            promote_candidate(self.unit, self.candidate.name, 'v1')
        self.assertFalse((self.unit / 'versions/v1').exists())

    def test_promotion_rechecks_stored_artifact_hash(self):
        self.attach()
        set_candidate_status(self.unit, self.candidate.name, 'ready')
        (self.unit / self.artifact['stored_path']).write_text('Changed result')
        with self.assertRaisesRegex(HarneloopError, 'changed'):
            promote_candidate(self.unit, self.candidate.name, 'v1')

    def test_unchanged_run_evidence_allows_normal_promotion(self):
        self.attach()
        set_candidate_status(self.unit, self.candidate.name, 'ready')
        promote_candidate(self.unit, self.candidate.name, 'v1')
        self.assertEqual((self.unit / 'agent-facing/policy.md').read_text(), 'Tested policy')
