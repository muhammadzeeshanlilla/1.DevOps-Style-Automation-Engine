import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from fastapi.testclient import TestClient
from api.app import create_app
from api.service import EngineAPIService
from config.loader import validate_config
from utils.process_manager import ProcessManager
from utils.runtime_status import RuntimeStatus


def configuration(folder):
    return validate_config({
        "schema_version": 2,
        "email": {"sender": "private@example.com", "receiver": "receiver@example.com",
                  "smtp_server": "smtp.example.com", "smtp_port": 587},
        "tasks": [
            {"id": "report", "name": "Private report name", "type": "folder_report", "enabled": True,
             "trigger": {"type": "daily", "hour": 10, "minute": 30}, "parameters": {"path": str(folder)}},
            {"id": "email-job", "name": "Private email name", "type": "email", "enabled": True,
             "trigger": {"type": "interval", "every_minutes": 5},
             "parameters": {"subject": "Private subject", "body": "Private email body"}},
            {"id": "event-job", "name": "Private event name", "type": "email", "enabled": False,
             "trigger": {"type": "file_event", "path": str(folder), "events": ["new", "modified"]},
             "parameters": {"subject": "Private event subject", "body": "Private event body"}},
        ],
    })


class APITestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = ProcessManager(self.root)
        self.config = configuration(self.root)
        self.loader = Mock(return_value=self.config)
        self.launcher = Mock()
        self.log_path = self.root / "engine.log"
        self.service = EngineAPIService(self.manager, self.loader, self.log_path, self.launcher,
                                        start_timeout=0, stop_timeout=0)
        self.client = TestClient(create_app(self.service), base_url="http://localhost")

    def tearDown(self):
        self.client.close()
        self.manager.release()
        self.temp.cleanup()

    def publish(self, lifecycle="RUNNING"):
        self.manager.acquire()
        self.manager.publish_state(lifecycle)
        self.runtime = RuntimeStatus(self.config.tasks, self.root)
        self.runtime.bind(self.manager)
        self.runtime.publish(self.manager, lifecycle, "alive", [{"label": "monitor-1", "state": "alive"}], 2)
        return self.runtime.path
