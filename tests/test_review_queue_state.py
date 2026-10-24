from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path

from review_queue_state import QueueState


class QueueStateTests(unittest.TestCase):
    def temporary_root(self):
        root = Path(__file__).resolve().parents[1] / f".test-queue-state-{uuid.uuid4().hex}"
        root.mkdir()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        return root

    def test_skip_history_and_back_survive_restart(self):
        root = self.temporary_root()
        state = QueueState(root)
        state.record_visit("a", "A", {"queue": "Needs action", "page": 1})
        state.record_skip("a", "A", {"queue": "Needs action", "page": 1})
        state.record_visit("b", "B", {"queue": "All recipes", "page": 2})

        restarted = QueueState(root)
        self.assertEqual(restarted.back("b")["candidate_id"], "a")
        self.assertEqual(restarted.skipped()[0]["candidate_id"], "a")
        self.assertEqual(restarted.skipped()[0]["title"], "A")
        self.assertEqual(restarted.skipped()[0]["context"]["page"], 1)

    def test_back_uses_prior_distinct_visit_across_repeated_reruns(self):
        state = QueueState(self.temporary_root())
        state.record_visit("a", "A", {})
        state.record_visit("b", "B", {})
        state.record_visit("c", "C", {})
        self.assertEqual(state.back("c", record=True)["candidate_id"], "b")
        state.record_visit("b", "B", {})
        state.record_visit("b", "B", {})
        self.assertEqual(state.back("b", record=True)["candidate_id"], "a")
        state.record_visit("a", "A", {})
        self.assertIsNone(state.back("a"))

    def test_skipped_items_can_be_restored_without_clearing_history(self):
        state = QueueState(self.temporary_root())
        state.record_skip("a", "A", {"queue": "All recipes"})
        state.record_visit("a", "A", {"queue": "All recipes"})
        self.assertEqual([item["candidate_id"] for item in state.skipped()], ["a"])


if __name__ == "__main__":
    unittest.main()
