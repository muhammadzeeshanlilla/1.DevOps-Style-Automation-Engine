import unittest
import tempfile
from pathlib import Path
from datetime import datetime
from dataclasses import FrozenInstanceError
from tasks.events import FileEvent

class FileEventTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name).resolve()
    def test_supported_types_and_deleted_path(self):
        for kind in ("new", "modified", "deleted"):
            event = FileEvent(kind, self.folder, self.folder / "missing.txt", datetime.now())
            self.assertEqual(event.path.name, "missing.txt")
    def test_immutable(self):
        event = FileEvent("new", self.folder, self.folder / "a", datetime.now())
        with self.assertRaises(FrozenInstanceError): event.event_type = "deleted"
    def test_unknown_type_rejected(self):
        with self.assertRaises(ValueError): FileEvent("bad", self.folder, self.folder / "a", datetime.now())
    def test_timestamp_type_rejected(self):
        with self.assertRaises(TypeError): FileEvent("new", self.folder, self.folder / "a", "now")
    def test_nested_or_relative_entry_rejected(self):
        for path in (Path("a"), self.folder / "nested" / "a"):
            with self.assertRaises(ValueError): FileEvent("new", self.folder, path, datetime.now())
