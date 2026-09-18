import errno
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import utils.process_manager as module
from utils.paths import ENGINE_LOCK_PATH, ENGINE_PID_PATH, PROJECT_ROOT
from utils.process_manager import AlreadyRunning, LegacyPIDError, ProcessManagementError, ProcessManager, ProcessStatus


def windows_error(code=5, message="temporary file contention"):
    error = PermissionError(errno.EACCES, message)
    error.winerror = code
    return error


class ProcessManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="engine-process-test-")
        self.root = Path(self.temp.name).resolve()
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temp.cleanup)
        self.manager = ProcessManager(self.root)
        self.addCleanup(self.manager.release)
        forbidden = patch("os.kill", side_effect=AssertionError("Process signals are forbidden"))
        forbidden.start()
        self.addCleanup(forbidden.stop)

    def test_default_paths_are_project_root_paths(self):
        manager = ProcessManager()
        self.assertEqual(manager.lock_path, ENGINE_LOCK_PATH)
        self.assertEqual(manager.pid_path, ENGINE_PID_PATH)
        self.assertEqual(manager.runtime_dir, PROJECT_ROOT)

    def test_duplicate_acquisition_preserves_owner(self):
        self.manager.acquire()
        original = self.manager.pid_path.read_bytes()
        with self.assertRaises(AlreadyRunning):
            ProcessManager(self.root).acquire()
        self.assertEqual(self.manager.pid_path.read_bytes(), original)
        self.assertEqual(ProcessManager(self.root).inspect_status().state, "STARTING")

    def test_stale_metadata_is_replaced_only_after_lock_acquisition(self):
        self.manager.acquire()
        stale = self.manager.pid_path.read_text()
        old_id = self.manager.instance_id
        self.manager.release()
        self.manager.pid_path.write_text(stale)
        self.assertEqual(self.manager.inspect_status().state, "STOPPED")
        self.assertEqual(self.manager.pid_path.read_text(), stale)
        self.manager.acquire()
        self.assertNotEqual(self.manager.instance_id, old_id)

    def test_malformed_unlocked_metadata_can_be_recovered(self):
        for text in ("broken JSON", "[]", '{"pid": 1}', '{"state": []}'):
            with self.subTest(text=text):
                self.manager.pid_path.write_text(text)
                self.assertEqual(self.manager.inspect_status().state, "STOPPED")
                self.manager.acquire()
                self.assertEqual(self.manager.inspect_status().state, "STARTING")
                self.manager.release()

    def test_malformed_metadata_with_held_lock_is_unknown(self):
        self.manager.acquire()
        self.manager.pid_path.write_text("broken")
        client = ProcessManager(self.root)
        self.assertEqual(client.inspect_status().state, "UNKNOWN")
        self.assertEqual(client.request_stop(timeout=0).state, "UNKNOWN")
        self.assertFalse(list(self.root.glob("engine.stop.*")))

    def test_legacy_pid_is_preserved_and_never_targeted(self):
        self.manager.pid_path.write_text("12345")
        self.assertEqual(self.manager.inspect_status().state, "LEGACY")
        self.assertEqual(self.manager.request_stop(timeout=0).state, "LEGACY")
        with self.assertRaises(LegacyPIDError):
            self.manager.acquire()
        self.assertEqual(self.manager.pid_path.read_text(), "12345")
        self.assertFalse(self.manager.owns_instance)
        self.manager.pid_path.unlink()
        self.manager.acquire()  # The failed migration did not leave a lock held.

    def test_instance_specific_and_repeated_stop_requests(self):
        self.manager.acquire()
        wrong = self.manager._stop_path(uuid.uuid4().hex)
        wrong.touch()
        self.assertFalse(self.manager.stop_requested())
        client = ProcessManager(self.root)
        for _ in range(2):
            self.assertEqual(client.request_stop(timeout=0).state, "REQUESTED")
        own = self.manager._stop_path(self.manager.instance_id)
        self.assertTrue(own.exists())
        self.assertTrue(self.manager.stop_requested())
        self.manager.release()
        self.assertFalse(own.exists())
        self.assertTrue(wrong.exists())
        self.assertTrue(self.manager.lock_path.exists())

    def test_stop_of_stopped_engine_does_not_create_request(self):
        self.assertEqual(self.manager.request_stop(timeout=0).state, "STOPPED")
        self.assertFalse(list(self.root.glob("engine.stop.*")))

    def test_late_request_for_old_instance_cannot_stop_new_instance(self):
        self.manager.acquire()
        old_request = self.manager._stop_path(self.manager.instance_id)
        self.manager.release()
        old_request.touch()  # A previous caller delivered its request after exit.
        self.manager.acquire()
        self.assertFalse(self.manager.stop_requested())
        self.manager.release()
        self.assertTrue(old_request.exists())

    def test_cleanup_does_not_delete_replaced_metadata(self):
        self.manager.acquire()
        metadata = json.loads(self.manager.pid_path.read_text())
        metadata["instance_id"] = uuid.uuid4().hex
        replacement = json.dumps(metadata)
        self.manager.pid_path.write_text(replacement)
        self.manager.release()
        self.assertEqual(self.manager.pid_path.read_text(), replacement)
        self.assertFalse(self.manager.owns_instance)

    def test_failed_publication_releases_lock_and_removes_temporary_file(self):
        with patch.object(module.os, "replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(ProcessManagementError):
                self.manager.acquire()
        self.assertFalse(self.manager.owns_instance)
        self.assertFalse(list(self.root.glob("engine.pid.*.tmp")))
        self.manager.acquire()

    def test_permission_error_is_unknown_not_stopped(self):
        with patch.object(self.manager, "_read_metadata", side_effect=PermissionError("denied")):
            self.assertEqual(self.manager.inspect_status().state, "UNKNOWN")
        self.manager.acquire()

    def test_runtime_cleanup_error_still_releases_lock(self):
        self.manager.acquire()
        with patch.object(self.manager, "_read_metadata", side_effect=PermissionError("denied")):
            with self.assertRaises(ProcessManagementError):
                self.manager.release()
        self.assertFalse(self.manager.owns_instance)
        self.manager.acquire()

    def test_invalid_identifier_cannot_escape_runtime_directory(self):
        with self.assertRaises(ProcessManagementError):
            self.manager._stop_path("../elsewhere")

    def test_windows_backend_uses_nonblocking_lock_not_process_signal(self):
        backend = Mock(LK_NBLCK=1, LK_UNLCK=2)
        lock = module._InstanceLock(self.root / "windows.lock")
        with patch.object(module, "WINDOWS", True), patch.object(module, "msvcrt", backend, create=True):
            self.assertTrue(lock.acquire())
            fd = lock.file.fileno()
            lock.release()
        self.assertEqual(backend.locking.call_args_list[0].args, (fd, 1, 1))
        self.assertEqual(backend.locking.call_args_list[1].args, (fd, 2, 1))

    def test_unix_backend_uses_nonblocking_file_lock(self):
        backend = Mock(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)
        lock = module._InstanceLock(self.root / "unix.lock")
        with patch.object(module, "WINDOWS", False), patch.object(module, "fcntl", backend, create=True):
            self.assertTrue(lock.acquire())
            fd = lock.file.fileno()
            lock.release()
        self.assertEqual(backend.flock.call_args_list[0].args, (fd, 6))
        self.assertEqual(backend.flock.call_args_list[1].args, (fd, 8))

    def test_contention_is_distinct_from_other_lock_errors(self):
        backend = Mock(LK_NBLCK=1, LK_UNLCK=2)
        with patch.object(module, "WINDOWS", True), patch.object(module, "msvcrt", backend, create=True):
            backend.locking.side_effect = OSError(errno.EACCES, "locked")
            lock = module._InstanceLock(self.root / "contention.lock")
            self.assertFalse(lock.acquire())
            self.assertIsNone(lock.file)
            backend.locking.side_effect = OSError(errno.EIO, "I/O failure")
            with self.assertRaises(OSError):
                lock.acquire()

    def test_windows_retry_recovers_for_all_selected_error_codes(self):
        for code in (5, 32, 33):
            with self.subTest(code=code), patch.object(module, "WINDOWS", True):
                operation = Mock(side_effect=[windows_error(code), "success"])
                self.assertEqual(module._retry_file_operation(operation), "success")
                self.assertEqual(operation.call_count, 2)

    def test_retry_expires_preserving_first_permission_error(self):
        first = windows_error(message="original denial")
        operation = Mock(side_effect=[first] + [windows_error(32)] * 20)
        with patch.object(module, "WINDOWS", True), patch.object(module, "FILE_RETRY_TIMEOUT", 0.025):
            with self.assertRaises(PermissionError) as caught:
                module._retry_file_operation(operation)
        self.assertIs(caught.exception, first)
        self.assertGreater(operation.call_count, 1)

    def test_windows_crt_access_denied_without_winerror_is_retried(self):
        operation = Mock(side_effect=[PermissionError(errno.EACCES, "CRT sharing denial"), "success"])
        with patch.object(module, "WINDOWS", True):
            self.assertEqual(module._retry_file_operation(operation), "success")
        self.assertEqual(operation.call_count, 2)

    def test_persistent_crt_permission_denial_is_not_hidden(self):
        error = PermissionError(errno.EACCES, "permanent permission denial")
        operation = Mock(side_effect=error)
        with patch.object(module, "WINDOWS", True), patch.object(module, "FILE_RETRY_TIMEOUT", 0.025):
            with self.assertRaises(PermissionError) as caught:
                module._retry_file_operation(operation)
        self.assertIs(caught.exception, error)
        self.assertGreater(operation.call_count, 1)

    def test_unrelated_filesystem_errors_are_not_retried(self):
        for error in (OSError(errno.EIO, "I/O failure"), OSError(errno.ENOSPC, "disk full"), PermissionError("no Windows contention code")):
            with self.subTest(error=str(error)), patch.object(module, "WINDOWS", True):
                operation = Mock(side_effect=error)
                with self.assertRaises(OSError) as caught:
                    module._retry_file_operation(operation)
                self.assertIs(caught.exception, error)
                operation.assert_called_once()

    def test_unix_errors_do_not_take_windows_retry_path(self):
        operation = Mock(side_effect=windows_error())
        with patch.object(module, "WINDOWS", False):
            with self.assertRaises(PermissionError):
                module._retry_file_operation(operation)
        operation.assert_called_once()

    def test_metadata_open_retries_then_reads_valid_record(self):
        self.manager.acquire()
        real_open = Path.open
        attempts = []
        def open_file(path, *args, **kwargs):
            attempts.append(path)
            if len(attempts) == 1:
                raise windows_error(32)
            return real_open(path, *args, **kwargs)
        with patch.object(module, "WINDOWS", True), patch.object(Path, "open", open_file):
            kind, metadata = self.manager._read_metadata()
        self.assertEqual(kind, "valid")
        self.assertEqual(metadata["instance_id"], self.manager.instance_id)
        self.assertEqual(len(attempts), 2)

    def test_replacement_deadline_keeps_old_metadata_and_reuses_temporary(self):
        self.manager.acquire()
        original = self.manager.pid_path.read_bytes()
        error = windows_error()
        with patch.object(module, "WINDOWS", True), patch.object(module, "FILE_RETRY_TIMEOUT", 0.025), patch.object(module.os, "replace", side_effect=error) as replace:
            with self.assertRaises(ProcessManagementError) as caught:
                self.manager.publish_state("RUNNING")
        self.assertIs(caught.exception.__cause__, error)
        self.assertEqual(len({call.args for call in replace.call_args_list}), 1)
        self.assertGreater(replace.call_count, 1)
        self.assertEqual(self.manager.pid_path.read_bytes(), original)
        self.assertTrue(self.manager.owns_instance)
        self.assertEqual(list(self.root.glob("engine.pid.*.tmp")), [])

    def test_temporary_cleanup_does_not_mask_publication_failure(self):
        publication = OSError(errno.ENOSPC, "publication failed")
        cleanup = OSError(errno.EIO, "cleanup failed")
        real_unlink = Path.unlink
        def unlink(path, *args, **kwargs):
            if path.suffix == ".tmp":
                raise cleanup
            return real_unlink(path, *args, **kwargs)
        with patch.object(module.os, "replace", side_effect=publication), patch.object(Path, "unlink", unlink):
            with self.assertRaises(ProcessManagementError) as caught:
                self.manager.acquire()
        self.assertIs(caught.exception.__cause__, publication)
        self.assertIs(publication.__cause__, cleanup)
        self.assertFalse(self.manager.owns_instance)

    @unittest.skipUnless(module.WINDOWS, "Real Windows sharing semantics required")
    def test_replacement_blocked_by_reader_succeeds_after_close(self):
        self.manager.acquire()
        reader = self.manager.pid_path.open("r")
        retry = threading.Event()
        sleep = time.sleep
        def pause(seconds):
            retry.set()
            sleep(seconds)
        def close():
            retry.wait(timeout=2)
            reader.close()
        closer = threading.Thread(target=close)
        closer.start()
        try:
            with patch.object(module.time, "sleep", side_effect=pause):
                self.manager.publish_state("RUNNING")
            self.assertTrue(retry.is_set())
            self.assertEqual(self.manager._read_metadata()[1]["state"], "RUNNING")
        finally:
            reader.close()
            closer.join(timeout=3)

    @unittest.skipUnless(module.WINDOWS, "Real Windows sharing semantics required")
    def test_deletion_blocked_by_reader_succeeds_after_close(self):
        self.manager.acquire()
        reader = self.manager.pid_path.open("r")
        retry = threading.Event()
        sleep = time.sleep
        def pause(seconds):
            retry.set()
            sleep(seconds)
        closer = threading.Thread(target=lambda: (retry.wait(timeout=2), reader.close()))
        closer.start()
        try:
            with patch.object(module.time, "sleep", side_effect=pause):
                self.manager.release()
            self.assertTrue(retry.is_set())
            self.assertFalse(self.manager.pid_path.exists())
        finally:
            reader.close()
            closer.join(timeout=3)

    @unittest.skipUnless(module.WINDOWS, "Real Windows sharing semantics required")
    def test_deletion_deadline_reports_error_but_releases_ownership(self):
        self.manager.acquire()
        with self.manager.pid_path.open("r"), patch.object(module, "FILE_RETRY_TIMEOUT", 0.025):
            with self.assertRaises(ProcessManagementError) as caught:
                self.manager.release()
        self.assertIn("runtime cleanup failed", str(caught.exception))
        self.assertFalse(self.manager.owns_instance)
        self.assertTrue(self.manager.pid_path.exists())
        self.manager.acquire()

    @unittest.skipUnless(module.WINDOWS, "Real Windows sharing semantics required")
    def test_stop_request_deletion_retries_sharing_error(self):
        self.manager.acquire()
        ProcessManager(self.root).request_stop(timeout=0)
        request = self.manager._stop_path(self.manager.instance_id)
        real_unlink = Path.unlink
        attempts = []
        def unlink(path, *args, **kwargs):
            if path == request:
                attempts.append(path)
                if len(attempts) == 1:
                    raise windows_error(32)
            return real_unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", unlink):
            self.manager.release()
        self.assertEqual(len(attempts), 2)
        self.assertFalse(request.exists())

    def test_metadata_disappears_after_probe_and_ownership_is_rechecked(self):
        self.manager.acquire()
        client = ProcessManager(self.root)
        real_read = client._read_metadata
        first = [True]
        def read(deadline=None):
            if first[0]:
                first[0] = False
                self.manager.release()
            return real_read(deadline=deadline)
        with patch.object(client, "_read_metadata", side_effect=read):
            self.assertEqual(client.inspect_status().state, "STOPPED")

    def test_missing_metadata_does_not_mean_held_owner_stopped(self):
        self.manager.acquire()
        self.manager.pid_path.unlink()
        with patch.object(module, "STATUS_RECHECK_TIMEOUT", 0.025):
            self.assertEqual(ProcessManager(self.root).inspect_status().state, "UNKNOWN")
        self.assertTrue(self.manager.owns_instance)

    def test_missing_metadata_gets_fresh_probe_even_with_expired_wait_budget(self):
        self.manager.acquire()
        client = ProcessManager(self.root)
        real_read = client._read_metadata
        first = [True]
        def read(deadline=None):
            if first[0]:
                first[0] = False
                self.manager.release()
            return real_read(deadline=deadline)
        with patch.object(client, "_read_metadata", side_effect=read):
            self.assertEqual(client.inspect_status(deadline=time.monotonic()).state, "STOPPED")

    def test_metadata_deleted_before_unlock_is_rechecked(self):
        self.manager.acquire()
        release = self.manager._lock.release
        thread = None
        def delayed_release():
            nonlocal thread
            thread = threading.Thread(target=lambda: (time.sleep(0.02), release()))
            thread.start()
        with patch.object(self.manager._lock, "release", side_effect=delayed_release):
            self.manager.release()
            try:
                self.assertEqual(ProcessManager(self.root).inspect_status().state, "STOPPED")
            finally:
                thread.join(timeout=3)

    def test_stop_polls_through_unknown_then_confirms_exit(self):
        self.manager.acquire()
        client = ProcessManager(self.root)
        initial = ProcessStatus("STARTING", dict(self.manager._metadata))
        with patch.object(client, "inspect_status", side_effect=[initial, ProcessStatus("UNKNOWN", detail="temporary gap"), ProcessStatus("STOPPED")]) as inspect:
            self.assertEqual(client.request_stop(timeout=0.1, poll_interval=0.001).state, "STOPPED")
        self.assertEqual(inspect.call_count, 3)
        self.assertTrue(self.manager.stop_requested())

    def test_persistent_unknown_times_out_with_diagnostic(self):
        self.manager.acquire()
        client = ProcessManager(self.root)
        calls = [0]
        def inspect(**kwargs):
            calls[0] += 1
            if calls[0] == 1:
                return ProcessStatus("STARTING", dict(self.manager._metadata))
            return ProcessStatus("UNKNOWN", detail="persistent permission problem")
        with patch.object(client, "inspect_status", side_effect=inspect):
            result = client.request_stop(timeout=0.025, poll_interval=0.001)
        self.assertEqual(result.state, "REQUESTED")
        self.assertIn("persistent permission problem", result.detail)
        self.assertGreater(calls[0], 2)

    def test_windows_read_retries_share_stop_timeout(self):
        self.manager.acquire()
        client = ProcessManager(self.root)
        with patch.object(module, "WINDOWS", True), patch.object(Path, "open", side_effect=windows_error(32)):
            begin = time.monotonic()
            result = client.request_stop(timeout=0.025)
            elapsed = time.monotonic() - begin
        self.assertEqual(result.state, "UNKNOWN")
        self.assertLess(elapsed, 0.3)
        self.assertFalse(self.manager.stop_requested())

    def test_start_recovers_after_temporary_status_probe_ownership(self):
        probe = module._InstanceLock(self.manager.lock_path)
        self.assertTrue(probe.acquire())
        release = threading.Thread(target=lambda: (time.sleep(0.02), probe.release()))
        release.start()
        try:
            self.manager.acquire()
            self.assertTrue(self.manager.owns_instance)
            self.assertEqual(self.manager._read_metadata()[1]["state"], "STARTING")
        finally:
            release.join(timeout=3)
            probe.release()

    def test_busy_ownership_deadline_does_not_publish_or_unlock_another_owner(self):
        probe = module._InstanceLock(self.manager.lock_path)
        self.assertTrue(probe.acquire())
        try:
            with patch.object(module, "OWNERSHIP_RECHECK_TIMEOUT", 0.025):
                with self.assertRaises(AlreadyRunning):
                    self.manager.acquire()
            self.assertFalse(self.manager.owns_instance)
            self.assertFalse(self.manager.pid_path.exists())
            contender = module._InstanceLock(self.manager.lock_path)
            try:
                self.assertFalse(contender.acquire())
            finally:
                contender.release()
        finally:
            probe.release()


if __name__ == "__main__":
    unittest.main()
