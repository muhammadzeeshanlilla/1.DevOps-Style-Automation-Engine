import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tasks.file_monitor import FileMonitor


class FileMonitorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="engine-monitor-test-")
        self.addCleanup(temp.cleanup)
        self.monitor = FileMonitor(str(Path(temp.name)))
        for replacement in (
            patch("tasks.file_monitor.logger"),
            patch("smtplib.SMTP", side_effect=AssertionError("Real email is forbidden")),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.addCleanup(self.monitor.stop, 3)

    def test_normal_start_stop_and_repeated_stop(self):
        self.assertTrue(self.monitor.start())
        self.assertTrue(self.monitor.running)
        self.assertFalse(self.monitor._thread.daemon)
        self.assertTrue(self.monitor.stop())
        self.assertFalse(self.monitor.running)
        self.assertTrue(self.monitor.stop())

    def test_stop_before_start(self):
        self.assertTrue(self.monitor.stop())
        self.assertIsNone(self.monitor._thread)
        self.assertTrue(self.monitor.start())

    def test_restart_creates_new_thread_without_discarding_events(self):
        self.monitor.start()
        first = self.monitor._thread
        self.monitor.stop()
        self.monitor._record_change("pending")
        self.assertTrue(self.monitor.start())
        self.assertIsNot(self.monitor._thread, first)
        self.assertFalse(self.monitor._stop_event.is_set())
        self.assertEqual(self.monitor.get_and_clear_changes(), ["pending"])

    def test_sequential_duplicate_start_keeps_same_worker_and_baseline(self):
        with patch.object(self.monitor, "_take_snapshot", return_value={}) as snapshot:
            self.monitor.start()
            first = self.monitor._thread
            with patch.object(self.monitor._stop_event, "clear", wraps=self.monitor._stop_event.clear) as clear:
                self.assertFalse(self.monitor.start())
                clear.assert_not_called()
            self.assertIs(self.monitor._thread, first)
            # Initial baseline plus the worker's first check, never another baseline.
            self.assertLessEqual(snapshot.call_count, 2)

    def test_simultaneous_duplicate_starts_create_only_one_worker(self):
        barrier = threading.Barrier(3)
        results = []
        def start():
            barrier.wait(timeout=3)
            results.append(self.monitor.start())
        callers = [threading.Thread(target=start) for _ in range(2)]
        for caller in callers:
            caller.start()
        barrier.wait(timeout=3)
        for caller in callers:
            caller.join(timeout=3)
            self.assertFalse(caller.is_alive())
        self.assertEqual(sorted(results), [False, True])

    def test_unfinished_worker_remains_running_and_cannot_restart(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def blocked_scan():
            entered.set()
            release.wait(timeout=3)
        with patch.object(self.monitor, "_check_changes", side_effect=blocked_scan), patch("tasks.file_monitor.logger") as logger:
            self.monitor.start()
            self.assertTrue(entered.wait(timeout=2))
            first = self.monitor._thread
            self.assertFalse(self.monitor.stop(timeout=0.01))
            self.assertTrue(self.monitor.running)
            logger.info.assert_any_call(f"File monitor started. Watching: {self.monitor.folder_path}")
            self.assertNotIn("File monitor stopped.", [call.args[0] for call in logger.info.call_args_list])
            self.assertFalse(self.monitor.start())
            self.assertIs(self.monitor._thread, first)
            self.assertTrue(self.monitor._stop_event.is_set())
            release.set()
            self.assertTrue(self.monitor.join(timeout=2))
            self.assertFalse(self.monitor.running)

    def test_failed_thread_start_can_be_cleaned_up_and_restarted(self):
        with patch("threading.Thread.start", side_effect=RuntimeError("start failed")):
            with self.assertRaises(RuntimeError):
                self.monitor.start()
        self.assertTrue(self.monitor.stop())
        self.assertTrue(self.monitor.start())

    def test_unexpected_worker_failure_is_logged(self):
        with patch.object(self.monitor, "_monitor_loop", side_effect=RuntimeError("worker failed")), patch("tasks.file_monitor.logger") as logger:
            self.monitor.start()
            self.assertTrue(self.monitor.join(timeout=2))
            logger.exception.assert_called_once_with("File monitor worker failed.")

    def test_monitor_still_waits_five_seconds(self):
        waited = threading.Event()
        real_wait = self.monitor._stop_event.wait
        intervals = []
        def wait(timeout):
            intervals.append(timeout)
            waited.set()
            return real_wait(timeout)
        with patch.object(self.monitor._stop_event, "wait", side_effect=wait):
            self.monitor.start()
            self.assertTrue(waited.wait(timeout=2))
            self.monitor.stop()
        self.assertEqual(intervals, [5])

    def test_detection_messages_and_baseline_are_preserved(self):
        self.monitor.known_files = {"deleted.txt": 1, "modified.txt": 1, "unchanged.txt": 1}
        current = {"new.txt": 2, "modified.txt": 2, "unchanged.txt": 1}
        with patch.object(self.monitor, "_take_snapshot", return_value=current):
            self.monitor._check_changes()
        self.assertEqual(self.monitor.get_and_clear_changes(), [
            "NEW file detected: new.txt", "DELETED file: deleted.txt", "MODIFIED file: modified.txt",
        ])
        self.assertEqual(self.monitor.known_files, current)
        self.assertEqual(self.monitor.get_and_clear_changes(), [])

    def test_append_during_collection_is_retained_for_next_batch(self):
        copied, release, attempting = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        class PausedList(list):
            def copy(self):
                result = super().copy()
                copied.set()
                release.wait(timeout=3)
                return result
        self.monitor.changes = PausedList(["earlier"])
        collected = []
        collector = threading.Thread(target=lambda: collected.extend(self.monitor.get_and_clear_changes()))
        def append():
            attempting.set()
            self.monitor._record_change("arriving")
        producer = threading.Thread(target=append)
        try:
            collector.start()
            self.assertTrue(copied.wait(timeout=2))
            producer.start()
            self.assertTrue(attempting.wait(timeout=2))
            self.assertTrue(self.monitor._changes_lock.locked())
        finally:
            release.set()
            collector.join(timeout=3)
            if producer.ident is not None:
                producer.join(timeout=3)
        self.assertFalse(collector.is_alive())
        self.assertFalse(producer.is_alive())
        self.assertEqual(collected, ["earlier"])
        self.assertEqual(self.monitor.get_and_clear_changes(), ["arriving"])

    def test_concurrent_recording_and_collection_returns_every_event_once(self):
        barrier = threading.Barrier(4)
        collected = []
        producer_done = [threading.Event(), threading.Event()]
        expected = {f"{producer}:{index}" for producer in range(2) for index in range(1000)}
        def produce(producer):
            barrier.wait(timeout=3)
            for index in range(1000):
                self.monitor._record_change(f"{producer}:{index}")
            producer_done[producer].set()
        def collect():
            barrier.wait(timeout=3)
            deadline = time.monotonic() + 3
            while not all(done.is_set() for done in producer_done) and time.monotonic() < deadline:
                collected.extend(self.monitor.get_and_clear_changes())
                producer_done[0].wait(timeout=0.001)
            collected.extend(self.monitor.get_and_clear_changes())
        threads = [threading.Thread(target=produce, args=(i,)) for i in range(2)]
        threads.append(threading.Thread(target=collect))
        for thread in threads:
            thread.start()
        barrier.wait(timeout=3)
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(collected), 2000)
        self.assertEqual(set(collected), expected)
        self.assertEqual(self.monitor.get_and_clear_changes(), [])
    def test_restore_places_older_batch_before_newer_events(self):
        batch = ["A", "B"]
        self.monitor._record_change("C")
        self.monitor.restore_changes(batch)
        self.assertEqual(self.monitor.get_and_clear_changes(), ["A", "B", "C"])
        self.assertEqual(batch, ["A", "B"])
        self.assertEqual(self.monitor.get_and_clear_changes(), [])

    def test_restore_preserves_identical_legitimate_events(self):
        self.monitor._record_change("same event")
        self.monitor.restore_changes(["same event", "same event"])
        self.assertEqual(self.monitor.get_and_clear_changes(), ["same event"] * 3)

    def test_empty_restore_preserves_new_events(self):
        self.monitor._record_change("new event")
        self.monitor.restore_changes([])
        self.assertEqual(self.monitor.get_and_clear_changes(), ["new event"])

    def test_concurrent_record_restore_and_collect_has_no_loss_or_extra_events(self):
        barrier = threading.Barrier(4)
        done = [threading.Event(), threading.Event()]
        collected = []
        expected = {f"new:{i}" for i in range(1000)} | {f"old:{i}" for i in range(1000)}
        def record():
            barrier.wait(timeout=3)
            for i in range(1000):
                self.monitor._record_change(f"new:{i}")
            done[0].set()
        def restore():
            barrier.wait(timeout=3)
            for i in range(0, 1000, 10):
                self.monitor.restore_changes([f"old:{j}" for j in range(i, i + 10)])
            done[1].set()
        def collect():
            barrier.wait(timeout=3)
            deadline = time.monotonic() + 3
            while not all(event.is_set() for event in done) and time.monotonic() < deadline:
                collected.extend(self.monitor.get_and_clear_changes())
                done[0].wait(timeout=0.001)
        threads = [threading.Thread(target=target) for target in (record, restore, collect)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=3)
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
        self.assertTrue(all(event.is_set() for event in done))
        collected.extend(self.monitor.get_and_clear_changes())
        self.assertEqual(len(collected), 2000)
        self.assertEqual(set(collected), expected)
        self.assertEqual(self.monitor.get_and_clear_changes(), [])


if __name__ == "__main__":
    unittest.main()
