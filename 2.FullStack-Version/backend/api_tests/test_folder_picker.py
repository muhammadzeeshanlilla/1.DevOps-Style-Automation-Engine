import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient

from api.app import create_app
from api.folder_picker import NativeFolderPicker, PickerResult
from api.service import EngineAPIService
from api_tests.helpers import configuration
from utils.process_manager import ProcessManager


class FolderPickerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = ProcessManager(self.root)
        self.picker = Mock()
        self.service = EngineAPIService(
            manager=self.manager,
            config_loader=lambda: configuration(self.root),
            config_path=self.root / "unused.json",
            folder_picker=self.picker,
        )
        self.client = TestClient(create_app(self.service), base_url="http://localhost")

    def tearDown(self):
        self.client.close()
        self.manager.release()
        self.temporary.cleanup()

    def test_successful_valid_selection(self):
        self.picker.select.return_value = PickerResult(selected=True, path=str(self.root))
        response = self.client.post("/api/folders/select")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "selected": True, "path": str(self.root), "valid": True,
            "available": True, "message": "",
        })

    def test_cancellation_is_not_an_error(self):
        self.picker.select.return_value = PickerResult()
        response = self.client.post("/api/folders/select")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["selected"])
        self.assertIsNone(response.json()["path"])
        self.assertEqual(response.json()["message"], "")

    def test_selected_invalid_directory_uses_existing_validation(self):
        missing = self.root / "missing"
        self.picker.select.return_value = PickerResult(selected=True, path=str(missing))
        response = self.client.post("/api/folders/select")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["selected"])
        self.assertFalse(response.json()["valid"])
        self.assertEqual(response.json()["path"], str(missing))

    def test_unavailable_picker_has_manual_fallback(self):
        self.picker.select.return_value = PickerResult(
            available=False,
            message="Folder picker is unavailable. Enter the folder path manually.",
        )
        data = self.client.post("/api/folders/select").json()
        self.assertFalse(data["selected"])
        self.assertFalse(data["available"])
        self.assertIn("manually", data["message"])

    def test_response_never_contains_directory_contents(self):
        private_file = self.root / "private-secret-name.txt"
        private_file.write_text("private contents", encoding="utf-8")
        self.picker.select.return_value = PickerResult(selected=True, path=str(self.root))
        response = self.client.post("/api/folders/select")
        self.assertNotIn(private_file.name, response.text)
        self.assertNotIn("private contents", response.text)

    def test_native_picker_internal_failure_is_safe(self):
        runner = Mock(side_effect=OSError("private system detail"))
        result = NativeFolderPicker(runner=runner).select()
        self.assertFalse(result.available)
        self.assertNotIn("private", result.message)

    def test_native_picker_decodes_cancel(self):
        completed = Mock(stdout='{"selected": false, "path": null}', returncode=0)
        result = NativeFolderPicker(runner=Mock(return_value=completed)).select()
        self.assertFalse(result.selected)
        self.assertTrue(result.available)

    def test_native_picker_decodes_selected_path(self):
        completed = Mock(stdout='{"selected": true, "path": "C:\\\\Folder"}', returncode=0)
        result = NativeFolderPicker(runner=Mock(return_value=completed)).select()
        self.assertTrue(result.selected)
        self.assertEqual(result.path, "C:\\Folder")

    def test_concurrent_picker_request_does_not_open_second_dialog(self):
        runner = Mock()
        picker = NativeFolderPicker(runner=runner)
        picker._lock.acquire()
        try:
            result = picker.select()
        finally:
            picker._lock.release()
        self.assertFalse(result.selected)
        self.assertIn("already open", result.message)
        runner.assert_not_called()

    def test_cross_site_request_is_rejected_before_picker(self):
        response = self.client.post(
            "/api/folders/select", headers={"Origin": "https://evil.example"}
        )
        self.assertEqual(response.status_code, 403)
        self.picker.select.assert_not_called()
