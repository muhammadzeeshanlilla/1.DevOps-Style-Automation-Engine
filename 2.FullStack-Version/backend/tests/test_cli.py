import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import main
import cli.handler as handler
from utils.paths import PROJECT_ROOT
from utils.process_manager import ProcessManager


# Test-only child: no real worker threads, file scans or SMTP connections.
# Runtime files are isolated, and a 15-second deadline ensures automatic exit.
CHILD = r'''
import os,sys,time
from pathlib import Path
from unittest.mock import patch
root,runtime,command,mode=sys.argv[1:]
sys.path.insert(0,root)
import main
import cli.handler as handler
from utils.process_manager import ProcessManager
from config.loader import validate_config
runtime=Path(runtime)
main.CONFIG_PATH=runtime/'invalid-settings.json'
handler.CONFIG_PATH=main.CONFIG_PATH
class Worker:
    def __init__(self,name): self.name=name
    def start(self):
        if mode=='crash' and self.name=='scheduler':
            os._exit(7)  # Test-owned process exits itself, without any signal.
        if mode=='failure' and self.name=='scheduler':
            raise RuntimeError('simulated scheduler startup failure')
    def request_stop(self): pass
    def is_running(self): return False
    def join(self,timeout=1): return True
    def stop(self,timeout=1):
        with (runtime/'cleanup.txt').open('a') as file:
            file.write(self.name+'\n')
        return True
manager=ProcessManager(runtime)
if mode=='cleanup-gap':
    from utils.process_manager import _InstanceLock
    unlock=_InstanceLock.release
    def delayed_unlock(lock):
        if lock.file is not None:
            time.sleep(0.2)
        unlock(lock)
    _InstanceLock.release=delayed_unlock
deadline=time.monotonic()+15
real_stop_requested=manager.stop_requested
manager.stop_requested=lambda: real_stop_requested() or time.monotonic()>=deadline
with patch.object(main,'FileMonitor',side_effect=lambda path:Worker('monitor')), \
     patch.object(main,'JobScheduler',side_effect=lambda config,monitor:Worker('scheduler')), \
     patch('os.kill',side_effect=AssertionError('Process signals are forbidden')), \
     patch('smtplib.SMTP',side_effect=AssertionError('Real email is forbidden')):
    if command=='start' and mode!='invalid':
        with patch.object(main,'load_config',return_value=validate_config({'watch_folder':str(runtime), 'schedule':{'hour':12,'minute':35},
            'email':{'sender':'sender@example.com','receiver':'receiver@example.com','smtp_server':'smtp.example.com','smtp_port':587}})):
            result=main.run_cli([command],manager)
    else:
        result=main.run_cli([command],manager)
sys.exit(result)
'''


STATUS_READER_CHILD = r'''
import json,sys,time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
from utils.process_manager import ProcessManager
root=Path(sys.argv[2]); manager=ProcessManager(root)
counts={}; diagnostics=[]; deadline=time.monotonic()+25
print('READY',flush=True)
with patch('os.kill',side_effect=AssertionError('Signals forbidden')), \
     patch('smtplib.SMTP',side_effect=AssertionError('Real email forbidden')):
    while time.monotonic()<deadline and not (root/'readers.done').exists():
        if manager.pid_path.exists():
            result=manager.inspect_status()
            counts[result.state]=counts.get(result.state,0)+1
            if result.state=='UNKNOWN' and len(diagnostics)<5:
                diagnostics.append(result.detail)
        time.sleep(0.001)
print(json.dumps({'counts':counts,'diagnostics':diagnostics}),flush=True)
'''


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="engine-cli-test-")
        self.root = Path(self.temp.name).resolve()
        self.assertEqual(self.root.parent, Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.temp.cleanup)
        self.manager = ProcessManager(self.root)
        self.addCleanup(self.manager.release)
        self.invalid = self.root / "invalid-settings.json"
        self.invalid.write_text("invalid JSON")

    def test_status_and_stop_do_not_load_workflow_or_construct_engine(self):
        with patch.object(main, "load_config", side_effect=AssertionError("Config must not be required")), patch.object(main, "Engine", side_effect=AssertionError("No engine should be constructed")), patch("os.kill", side_effect=AssertionError("Signals forbidden")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.run_cli(["status"], self.manager), 0)
            self.assertEqual(main.run_cli(["stop"], self.manager), 0)

    def test_running_status_tolerates_missing_or_invalid_config(self):
        self.manager.acquire()
        self.manager.publish_state("RUNNING")
        for path in [self.invalid, self.root / "missing.json"]:
            output = io.StringIO()
            with patch.object(handler, "CONFIG_PATH", path), contextlib.redirect_stdout(output):
                self.assertEqual(main.run_cli(["status"], ProcessManager(self.root)), 0)
            self.assertIn("RUNNING", output.getvalue())
            self.assertIn("unavailable or invalid", output.getvalue())

    def test_stop_tolerates_invalid_config_and_timeout_is_not_success(self):
        self.manager.acquire()
        with patch.object(main, "CONFIG_PATH", self.invalid), patch.object(handler, "CONFIG_PATH", self.invalid), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(handler.handle_command("stop", process_manager=ProcessManager(self.root), stop_timeout=0), 1)
        self.assertTrue(self.manager.stop_requested())
        self.assertIn("not confirmed", output.getvalue())

    def test_unknown_command_does_not_require_config(self):
        with patch.object(main, "load_config", side_effect=AssertionError("No config")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main.run_cli(["unknown"], self.manager), 2)
            self.assertEqual(main.run_cli([], self.manager), 2)

    def test_status_from_another_directory_uses_same_instance(self):
        self.manager.acquire()
        original = Path.cwd()
        try:
            os.chdir(self.root)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main.run_cli(["status"], ProcessManager(self.root)), 0)
            self.assertIn("STARTING", output.getvalue())
        finally:
            os.chdir(original)

    def _arguments(self, command, mode="normal"):
        return [sys.executable, "-B", "-c", CHILD, str(PROJECT_ROOT), str(self.root), command, mode]

    def _run_child(self, command, mode="normal"):
        return subprocess.run(self._arguments(command, mode), cwd=PROJECT_ROOT.parent, capture_output=True, text=True, timeout=8)

    def _finish_child(self, process):
        if process.poll() is None:
            self.manager.request_stop(timeout=3)
        # The child also cooperatively stops itself after 15 seconds.
        process.communicate(timeout=20)

    def test_controlled_subprocess_start_duplicate_status_and_stop(self):
        process = subprocess.Popen(self._arguments("start"), cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 8
            while self.manager.inspect_status().state != "RUNNING":
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    self.fail("Controlled engine exited before RUNNING: " + stdout + stderr)
                self.assertLess(time.monotonic(), deadline, "Controlled engine did not start")
                time.sleep(0.05)
            original = self.manager.pid_path.read_bytes()
            status = self._run_child("status")
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("RUNNING", status.stdout)
            self.assertIn("Tasks loaded: 1 enabled", status.stdout)
            self.assertIn("Scheduler: stopped", status.stdout)
            self.assertIn("unavailable or invalid", status.stdout)
            duplicate = self._run_child("start")
            self.assertEqual(duplicate.returncode, 1, duplicate.stderr)
            self.assertIn("already running", duplicate.stdout)
            self.assertEqual(self.manager.pid_path.read_bytes(), original)
            stop = self._run_child("stop")
            self.assertEqual(stop.returncode, 0, stop.stdout + stop.stderr)
            self.assertIn("released ownership", stop.stdout)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertIn("cleanup completed", stdout)
            self.assertEqual((self.root / "cleanup.txt").read_text().splitlines(), ["monitor", "scheduler"])
            self.assertFalse(self.manager.pid_path.exists())
            self.assertFalse((self.root / "engine.status.json").exists())
            self.assertFalse(list(self.root.glob("engine.stop.*")))
            self.assertEqual(self._run_child("status").returncode, 0)
            self.assertEqual(self._run_child("stop").returncode, 0)
        finally:
            self._finish_child(process)

    def test_controlled_stop_confirms_shutdown_across_cleanup_gap(self):
        process = subprocess.Popen(self._arguments("start", "cleanup-gap"), cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 8
            while self.manager.inspect_status().state != "RUNNING":
                if process.poll() is not None:
                    stdout, stderr = process.communicate()
                    self.fail("Controlled engine exited early: " + stdout + stderr)
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.005)
            result = self.manager.request_stop(timeout=4, poll_interval=0.005)
            self.assertEqual(result.state, "STOPPED", result.detail)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertFalse(self.manager.pid_path.exists())
        finally:
            self._finish_child(process)

    def test_controlled_subprocess_startup_failure_cleans_runtime_state(self):
        result = self._run_child("start", "failure")
        self.assertEqual(result.returncode, 1)
        self.assertEqual((self.root / "cleanup.txt").read_text().splitlines(), ["monitor", "scheduler"])
        self.assertFalse(self.manager.pid_path.exists())
        self.assertEqual(self.manager.inspect_status().state, "STOPPED")

    def test_controlled_abrupt_exit_releases_lock_and_leaves_recoverable_state(self):
        result = self._run_child("start", "crash")
        self.assertEqual(result.returncode, 7)
        self.assertTrue(self.manager.pid_path.exists())
        self.assertEqual(self.manager.inspect_status().state, "STOPPED")
        old_id = json.loads(self.manager.pid_path.read_text())["instance_id"]
        self.manager.acquire()
        self.assertNotEqual(self.manager.instance_id, old_id)

    def test_two_simultaneous_subprocess_starts_have_only_one_owner(self):
        processes = [subprocess.Popen(self._arguments("start"), cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        try:
            deadline = time.monotonic() + 8
            while sum(process.poll() is not None for process in processes) == 0:
                self.assertLess(time.monotonic(), deadline, "Competing start was not rejected")
                time.sleep(0.05)
            self.assertEqual(sorted(process.poll() for process in processes if process.poll() is not None), [1])
            while self.manager.inspect_status().state != "RUNNING":
                self.assertLess(time.monotonic(), deadline, "Owning engine did not reach RUNNING")
                time.sleep(0.05)
            self.assertEqual(self.manager.inspect_status().state, "RUNNING")
            self.assertEqual(self._run_child("stop").returncode, 0)
            for process in processes:
                process.communicate(timeout=5)
            self.assertEqual(sorted(process.returncode for process in processes), [0, 1])
        finally:
            for process in processes:
                self._finish_child(process)

    def test_controlled_subprocess_bad_start_config_does_not_create_pid(self):
        result = self._run_child("start", "invalid")
        self.assertEqual(result.returncode, 1)
        self.assertIn("invalid JSON", result.stdout)
        self.assertFalse(self.manager.pid_path.exists())


def run_stress(cycles=100, publications=1000):
    """Explicit, reproducible subprocess stress run; never part of routine discovery."""
    totals = {}
    failures = []
    def start_readers(root, count):
        readers = []
        # Register cooperative cleanup immediately, including if a later launch fails.
        try:
            for _ in range(count):
                child = subprocess.Popen(
                    [sys.executable, "-B", "-c", STATUS_READER_CHILD, str(PROJECT_ROOT), str(root)],
                    cwd=PROJECT_ROOT.parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                readers.append(child)
                if child.stdout.readline().strip() != "READY":
                    raise AssertionError("Status reader failed to become ready")
            return readers
        except BaseException:
            finish_readers(root, readers)
            raise
    def finish_readers(root, readers):
        (root / "readers.done").touch()
        diagnostics = []
        for reader in readers:
            stdout, stderr = reader.communicate(timeout=30)
            if reader.returncode != 0:
                diagnostics.append(stdout + stderr)
                continue
            result = json.loads(stdout)
            diagnostics.extend(result["diagnostics"])
            for state, count in result["counts"].items():
                totals[state] = totals.get(state, 0) + count
        if diagnostics:
            raise AssertionError("Status reader diagnostics: " + repr(diagnostics))
    class StressCase(CLITests):
        def setUp(self):
            super().setUp()
            readers = start_readers(self.root, 2)
            self.addCleanup(finish_readers, self.root, readers)
    started = time.monotonic()
    for index in range(1, cycles + 1):
        output = io.StringIO()
        case = StressCase("test_controlled_subprocess_start_duplicate_status_and_stop")
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(unittest.TestSuite([case]))
        if not result.wasSuccessful():
            failures.append({"cycle": index, "diagnostic": output.getvalue()})
            print(output.getvalue(), flush=True)
        if index % 10 == 0:
            print(f"Lifecycle stress: {index}/{cycles}; failures={len(failures)}", flush=True)
    lifecycle_counts = dict(totals)
    totals.clear()
    with tempfile.TemporaryDirectory(prefix="engine-publication-stress-") as directory:
        root = Path(directory)
        manager = ProcessManager(root)
        readers = []
        manager.acquire()
        try:
            readers = start_readers(root, 4)
            for _ in range(publications):
                manager.publish_state("RUNNING")
            manager.publish_state("STOPPING")
        finally:
            try:
                manager.release()
            finally:
                finish_readers(root, readers)
        if manager.inspect_status().state != "STOPPED" or manager.pid_path.exists():
            raise AssertionError("Publication stress left runtime ownership or metadata")
    overlap_output = io.StringIO()
    overlap = unittest.TextTestRunner(stream=overlap_output, verbosity=2).run(
        unittest.TestSuite([CLITests("test_controlled_stop_confirms_shutdown_across_cleanup_gap") for _ in range(10)])
    )
    if not overlap.wasSuccessful():
        failures.append({"shutdown_overlap": overlap_output.getvalue()})
    summary = {
        "cycles": cycles, "publications": publications, "shutdown_overlap_checks": 10,
        "failures": failures, "lifecycle_reader_counts": lifecycle_counts,
        "publication_reader_counts": dict(totals), "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    print(json.dumps(summary, indent=2), flush=True)
    if failures:
        raise AssertionError("Process stress verification failed; see diagnostics above")
    return summary


if __name__ == "__main__":
    unittest.main()
