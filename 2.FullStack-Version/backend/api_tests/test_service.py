import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from api.service import APIError, ENGINE_PYTHON, MAX_LOG_BYTES, public_id
from utils.paths import PROJECT_ROOT
from utils.process_manager import ProcessStatus
from api_tests.helpers import APITestCase


class ServiceTests(APITestCase):
    def child(self, state="STARTING"):
        process = Mock(pid=12345)
        process.poll.return_value = None
        self.launcher.return_value = process
        metadata = {"pid": process.pid, "instance_id": "a" * 32}
        self.manager.inspect_status = Mock(side_effect=[
            ProcessStatus("STOPPED"), ProcessStatus(state, metadata)])
        return process

    def test_existing_owner_blocks_start(self):
        self.manager.acquire()
        with self.assertRaises(APIError) as error:
            self.service.start()
        self.assertEqual(error.exception.status_code, 409)
        self.launcher.assert_not_called()
        self.assertTrue(self.manager.owns_instance)

    def test_legacy_owner_blocks_start(self):
        self.manager.pid_path.write_text("12345")
        with self.assertRaises(APIError):
            self.service.start()
        self.launcher.assert_not_called()
        self.assertEqual(self.manager.pid_path.read_text(), "12345")

    def test_unknown_ownership_blocks_start(self):
        self.manager.inspect_status = Mock(return_value=ProcessStatus("UNKNOWN", detail="private"))
        with self.assertRaises(APIError):
            self.service.start()
        self.launcher.assert_not_called()

    def test_pending_child_blocks_duplicate_start(self):
        self.service._child = Mock()
        self.service._child.poll.return_value = None
        with self.assertRaises(APIError):
            self.service.start()
        self.launcher.assert_not_called()

    def test_invalid_configuration_prevents_launch(self):
        self.loader.side_effect = ValueError("secret")
        with self.assertRaises(APIError) as error:
            self.service.start()
        self.assertEqual(error.exception.status_code, 503)
        self.launcher.assert_not_called()

    def test_start_launches_existing_main_with_direct_interpreter(self):
        self.child()
        self.assertEqual(self.service.start().state, "STARTING")
        args, options = self.launcher.call_args
        self.assertEqual(args[0], [ENGINE_PYTHON, "-B", str(PROJECT_ROOT / "main.py"), "start"])
        self.assertEqual(options["cwd"], str(PROJECT_ROOT))
        self.assertEqual(options["stdout"], subprocess.DEVNULL)
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertTrue(options["close_fds"])
        self.assertIn("env", options)
        self.assertIsNot(options["env"], os.environ)
        if os.name == "nt":
            self.assertEqual(options["creationflags"], subprocess.CREATE_NO_WINDOW)
        else:
            self.assertTrue(options["start_new_session"])

    def test_running_child_is_confirmed(self):
        self.child("RUNNING")
        self.assertEqual(self.service.start().state, "RUNNING")

    def test_windows_cli_avoids_virtual_environment_pid_redirector(self):
        import sys
        expected = getattr(sys, "_base_executable", sys.executable) if os.name == "nt" else sys.executable
        self.assertEqual(ENGINE_PYTHON, expected)

    def test_exited_child_does_not_claim_success(self):
        self.manager.inspect_status = Mock(return_value=ProcessStatus("STOPPED"))
        self.launcher.return_value.poll.return_value = 1
        with self.assertRaises(APIError) as error:
            self.service.start()
        self.assertEqual(error.exception.status_code, 503)

    def test_launch_failure_is_safe(self):
        self.launcher.side_effect = OSError("secret machine path")
        with self.assertRaises(APIError) as error:
            self.service.start()
        self.assertNotIn("secret", str(error.exception))

    def test_timeout_is_pending_not_running(self):
        self.manager.inspect_status = Mock(return_value=ProcessStatus("STOPPED"))
        self.launcher.return_value.poll.return_value = None
        self.assertEqual(self.service.start().state, "START_REQUESTED")
        with self.assertRaises(APIError):
            self.service.start()
        self.assertEqual(self.launcher.call_count, 1)

    def test_stop_uses_instance_request_and_retains_owner(self):
        self.manager.acquire()
        code, response = self.service.stop()
        self.assertEqual(code, 202)
        self.assertEqual(response.state, "REQUESTED")
        self.assertTrue(self.manager.stop_requested())
        self.assertTrue(self.manager.owns_instance)

    def test_stop_of_stopped_engine_is_idempotent(self):
        for _ in range(2):
            self.assertEqual(self.service.stop()[0], 200)
        self.assertEqual(list(self.root.glob("engine.stop.*")), [])

    def test_stop_rejects_legacy_owner(self):
        self.manager.pid_path.write_text("12345")
        with self.assertRaises(APIError):
            self.service.stop()
        self.assertEqual(list(self.root.glob("engine.stop.*")), [])

    def test_stop_rejects_unknown_owner(self):
        self.manager.request_stop = Mock(return_value=ProcessStatus("UNKNOWN", detail="secret path"))
        with self.assertRaises(APIError) as error:
            self.service.stop()
        self.assertNotIn("secret", str(error.exception))

    def test_stop_does_not_target_unowned_pending_start(self):
        self.service._child = Mock()
        self.service._child.poll.return_value = None
        with self.assertRaises(APIError):
            self.service.stop()
        self.assertEqual(list(self.root.glob("engine.stop.*")), [])

    def test_status_does_not_require_valid_configuration(self):
        self.loader.side_effect = ValueError("invalid config")
        self.assertEqual(self.service.status().state, "STOPPED")
        self.loader.assert_not_called()

    def test_public_id_preserves_redaction_and_bounds_untrusted_values(self):
        self.assertEqual(public_id("[REDACTED]-job"), "[REDACTED]-job")
        for value in ("private body with spaces", "a" * 257, None):
            self.assertEqual(public_id(value), "[UNAVAILABLE]")
        with patch.dict(os.environ, {"SMTP_PASSWORD": "secret"}):
            self.assertEqual(public_id("secret-job"), "[REDACTED]-job")

    def test_log_tail_has_byte_budget(self):
        self.log_path.write_bytes(b"x" * (MAX_LOG_BYTES + 100) + b"\n2026-09-18 12:00:00 [INFO] Task completed | private\n")
        data = self.service.logs(100)
        self.assertTrue(data.truncated)
        self.assertEqual(data.count, 1)
        self.assertEqual(data.entries[0].message, "Task completed.")

    def test_incomplete_log_record_is_not_returned(self):
        self.log_path.write_text("2026-09-18 12:00:00 [INFO] Task started | private")
        self.assertEqual(self.service.logs(100).count, 0)

    def test_invalid_log_timestamp_and_continuations_are_skipped(self):
        self.log_path.write_text("2026-99-99 12:00:00 [INFO] private\nTraceback private\n")
        self.assertEqual(self.service.logs(100).count, 0)

    def test_unknown_log_text_is_withheld(self):
        self.log_path.write_text("2026-09-18 12:00:00 [ERROR] arbitrary private body\n")
        self.assertEqual(self.service.logs(100).entries[0].message, "Additional log details withheld for privacy.")

    def test_snapshot_ids_are_redacted_at_read_time(self):
        self.publish()
        with patch.dict(os.environ, {"SMTP_PASSWORD": "email-job"}):
            data = self.service.status().model_dump_json()
        self.assertNotIn("email-job", data)
        self.assertIn("[REDACTED]", data)

    def test_stopped_engine_ignores_leftover_snapshot(self):
        self.publish()
        self.manager.release()
        self.assertFalse(self.service.status().runtime.available)

    def test_competing_owner_is_not_reported_as_our_start(self):
        self.child()
        self.manager.inspect_status.side_effect = [
            ProcessStatus("STOPPED"), ProcessStatus("RUNNING", {"pid": 99999})]
        with self.assertRaises(APIError) as error:
            self.service.start()
        self.assertEqual(error.exception.status_code, 409)

    def test_simultaneous_starts_launch_only_one_child(self):
        self.manager.inspect_status = Mock(return_value=ProcessStatus("STOPPED"))
        self.launcher.return_value.poll.return_value = None
        def attempt():
            try:
                return self.service.start().state
            except APIError:
                return "CONFLICT"
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: attempt(), range(2)))
        self.assertEqual(sorted(outcomes), ["CONFLICT", "START_REQUESTED"])
        self.assertEqual(self.launcher.call_count, 1)
