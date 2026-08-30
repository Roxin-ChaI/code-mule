import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from code_mule.state import (
    InvalidProjectState,
    JsonProjectStateStore,
    ProjectStateNotFound,
)

from state import make_project_state


class JsonProjectStateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "project-state.json"
        self.store = JsonProjectStateStore(self.path)

    def test_save_and_load_round_trip(self):
        state = make_project_state()
        self.store.save(state)
        self.assertEqual(self.store.load(), state)

        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertTrue(self.path.read_text(encoding="utf-8").endswith("\n"))

    def test_exists_is_false_before_save_and_true_after_save(self):
        self.assertFalse(self.store.exists())
        self.assertFalse(self.path.exists())
        self.store.save(make_project_state())
        self.assertTrue(self.store.exists())

    def test_load_missing_file_raises_project_state_not_found(self):
        with self.assertRaises(ProjectStateNotFound):
            self.store.load()

    def test_load_invalid_json_raises_invalid_project_state(self):
        self.path.write_text("{not valid json", encoding="utf-8")
        with self.assertRaises(InvalidProjectState):
            self.store.load()

    def test_save_atomically_replaces_existing_state(self):
        state_a = make_project_state(name="State A")
        state_b = make_project_state(name="State B")

        self.store.save(state_a)
        self.assertEqual(self.store.load(), state_a)
        self.store.save(state_b)

        self.assertEqual(self.store.load(), state_b)
        self.assertEqual(
            [item.name for item in Path(self.temporary_directory.name).iterdir()],
            ["project-state.json"],
        )

    def test_failed_serialization_preserves_existing_state(self):
        state = make_project_state(name="Preserved")
        self.store.save(state)

        with patch(
            "code_mule.state.store.serialize_project_state",
            side_effect=RuntimeError("serialization failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "serialization failed"):
                self.store.save(make_project_state(name="Not Saved"))

        self.assertEqual(self.store.load(), state)
        self.assertEqual(
            [item.name for item in Path(self.temporary_directory.name).iterdir()],
            ["project-state.json"],
        )


if __name__ == "__main__":
    unittest.main()
