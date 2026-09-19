"""Real CLI/Engine subprocesses, isolated runtime, idle tasks, no SMTP/network."""

import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from api.app import create_app
from api.service import ENGINE_PYTHON, EngineAPIService
from config.loader import validate_config
from utils.paths import PROJECT_ROOT
from utils.process_manager import ProcessManager
from api_tests.helpers import APITestCase

CHILD = """
import sys
sys.path.insert(0, sys.argv[1])
import main
import cli.handler
from config.loader import validate_config
from utils.process_manager import ProcessManager
main.load_config = lambda: validate_config({
    'schema_version': 2,
    'email': {'sender': 'test@example.com', 'receiver': 'test@example.com',
              'smtp_server': 'smtp.example.com', 'smtp_port': 587},
    'tasks': []})
cli.handler.load_config_for_status = lambda: None
sys.exit(main.run_cli([sys.argv[3]], process_manager=ProcessManager(sys.argv[2])))
"""


class IntegrationTests(APITestCase):
    def setUp(self):
        super().setUp()
        self.processes = []
        self.idle = validate_config({
            "schema_version": 2,
            "email": {"sender": "test@example.com", "receiver": "test@example.com",
                      "smtp_server": "smtp.example.com", "smtp_port": 587},
            "tasks": [],
        })
        self.service = EngineAPIService(self.manager, lambda: self.idle, self.log_path,
                                        self.launch, start_timeout=5, stop_timeout=5)
        self.client.close()
        self.client = TestClient(create_app(self.service), base_url="http://localhost")

    def command(self, verb):
        return [ENGINE_PYTHON, "-B", "-c", CHILD, str(PROJECT_ROOT), str(self.root), verb]

    def launch(self, ignored_command, **options):
        options.setdefault("stdin", subprocess.DEVNULL)
        options.setdefault("stdout", subprocess.DEVNULL)
        options.setdefault("stderr", subprocess.DEVNULL)
        if sys.platform == "win32":
            options.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
        process = subprocess.Popen(self.command("start"), **options)
        self.processes.append(process)
        return process

    def wait_running(self):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            if self.manager.inspect_status().state == "RUNNING":
                return
            time.sleep(0.05)
        self.fail("Isolated CLI engine did not reach RUNNING.")

    def tearDown(self):
        try:
            ProcessManager(self.root).request_stop(timeout=6)
            for process in self.processes:
                process.wait(timeout=8)
        finally:
            super().tearDown()

    def test_api_start_can_be_observed_and_stopped_by_cli(self):
        self.assertEqual(self.client.post("/api/engine/start").status_code, 202)
        self.wait_running()
        status = subprocess.run(self.command("status"), capture_output=True, text=True, timeout=8)
        self.assertEqual(status.returncode, 0)
        self.assertIn("RUNNING", status.stdout)
        stop = subprocess.run(self.command("stop"), capture_output=True, text=True, timeout=8)
        self.assertEqual(stop.returncode, 0)
        self.processes[0].wait(timeout=8)
        self.assertEqual(self.client.get("/api/status").json()["state"], "STOPPED")

    def test_cli_started_engine_can_be_stopped_by_api(self):
        child = self.launch([])
        self.wait_running()
        data = self.client.get("/api/status").json()
        self.assertEqual(data["pid"], child.pid)
        self.assertTrue(data["runtime"]["available"])
        self.assertEqual(self.client.post("/api/engine/stop").status_code, 200)
        child.wait(timeout=8)
        self.assertEqual(self.manager.inspect_status().state, "STOPPED")

    def test_api_and_cli_duplicate_starts_preserve_existing_owner(self):
        self.assertEqual(self.client.post("/api/engine/start").status_code, 202)
        self.wait_running()
        owner = self.manager.inspect_status().metadata["instance_id"]
        self.assertEqual(self.client.post("/api/engine/start").status_code, 409)
        duplicate = subprocess.run(self.command("start"), capture_output=True, text=True, timeout=8)
        self.assertEqual(duplicate.returncode, 1)
        self.assertEqual(self.manager.inspect_status().metadata["instance_id"], owner)

    def test_concurrent_api_instances_share_the_core_lock(self):
        second = EngineAPIService(ProcessManager(self.root), lambda: self.idle, self.log_path,
                                  self.launch, start_timeout=5, stop_timeout=5)
        with TestClient(create_app(second), base_url="http://localhost") as other:
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(lambda client: client.post("/api/engine/start"), [self.client, other]))
            self.assertEqual(sorted(response.status_code for response in responses), [202, 409])
            self.wait_running()
            # The losing CLI process must finish its duplicate-start cleanup;
            # it never owned the runtime or started a second set of workers.
            deadline = time.monotonic() + 5
            while sum(process.poll() is None for process in self.processes) > 1 and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(sum(process.poll() is None for process in self.processes), 1)

    def test_api_client_exit_does_not_stop_independent_cli_engine(self):
        self.assertEqual(self.client.post("/api/engine/start").status_code, 202)
        self.wait_running()
        self.client.close()
        self.assertEqual(self.manager.inspect_status().state, "RUNNING")
        result = subprocess.run(self.command("stop"), capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0)
