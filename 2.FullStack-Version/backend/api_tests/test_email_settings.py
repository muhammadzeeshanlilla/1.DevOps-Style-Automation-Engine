import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient

from api.app import create_app
from api.credential_store import CredentialStoreError
from api.service import EngineAPIService
from config.loader import load_config
from utils.process_manager import ProcessManager, ProcessStatus


class FakeCredentialStore:
    def __init__(self, available=True):
        self.values = {}
        self.is_available = available

    def available(self):
        return self.is_available

    def get(self, account):
        if not self.is_available:
            raise CredentialStoreError()
        return self.values.get(account)

    def set(self, account, secret):
        if not self.is_available:
            raise CredentialStoreError()
        self.values[account] = secret

    def delete(self, account):
        if not self.is_available:
            raise CredentialStoreError()
        self.values.pop(account, None)


class EmailSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.config_path = self.root / "settings.json"
        self.config_path.write_text(json.dumps({
            "schema_version": 2,
            "email": {
                "sender": "old@example.com", "receiver": "receiver@example.com",
                "smtp_server": "smtp.example.com", "smtp_port": 587,
            },
            "tasks": [],
        }), encoding="utf-8")
        self.manager = ProcessManager(self.runtime)
        self.credentials = FakeCredentialStore()
        self.sender = Mock(return_value=True)
        self.launcher = Mock()
        self.service = EngineAPIService(
            manager=self.manager, config_loader=lambda: load_config(self.config_path),
            config_path=self.config_path, log_path=self.root / "engine.log",
            credential_store=self.credentials, email_sender=self.sender,
            launcher=self.launcher, start_timeout=0,
        )
        self.client = TestClient(create_app(self.service), base_url="http://localhost")

    def tearDown(self):
        self.client.close()
        self.manager.release()
        self.temporary.cleanup()

    def update_body(self, password="group ed app pass word", remember=True):
        return {
            "sender": "sender@gmail.com",
            "receiver": "receiver@gmail.com",
            "smtp_server": "smtp.gmail.com",
            "smtp_port": 587,
            "app_password": password,
            "remember_credential": remember,
        }

    def test_read_returns_only_safe_settings(self):
        response = self.client.get("/api/email-settings")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sender"], "old@example.com")
        self.assertFalse(response.json()["credential_saved"])
        self.assertNotIn("app_password", response.text)

    def test_save_non_secret_settings_and_persisted_credential(self):
        response = self.client.put("/api/email-settings", json=self.update_body())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["credential_saved"])
        self.assertTrue(response.json()["credential_persisted"])
        config = load_config(self.config_path)
        self.assertEqual(config.email["sender"], "sender@gmail.com")
        self.assertEqual(self.credentials.values["sender@gmail.com"], "groupedapppassword")

    def test_password_is_never_written_or_returned(self):
        secret = "UNIQUE SECRET VALUE"
        response = self.client.put("/api/email-settings",
                                   json=self.update_body(password=secret))
        self.assertNotIn(secret, response.text)
        self.assertNotIn(secret, self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("password", self.config_path.read_text(encoding="utf-8").lower())

    def test_session_only_credential(self):
        response = self.client.put("/api/email-settings",
                                   json=self.update_body(password="session secret", remember=False))
        self.assertTrue(response.json()["credential_saved"])
        self.assertFalse(response.json()["credential_persisted"])
        self.assertEqual(self.service.email_settings.credential("sender@gmail.com"),
                         "sessionsecret")
        self.assertEqual(self.credentials.values, {})

    def test_sender_change_clears_old_session_credential(self):
        self.service.email_settings._session_credentials["old@example.com"] = "old-secret"
        self.assertEqual(self.client.put("/api/email-settings",
                         json=self.update_body(remember=False)).status_code, 200)
        self.assertNotIn("old@example.com",
                         self.service.email_settings._session_credentials)

    def test_sender_change_requires_old_persisted_credential_to_be_forgotten(self):
        self.credentials.values["old@example.com"] = "old-secret"
        response = self.client.put("/api/email-settings",
                                   json=self.update_body())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"],
            "Forget the saved App Password before changing sender email.",
        )

    def test_retrieve_persisted_credential(self):
        self.credentials.values["old@example.com"] = "persisted"
        response = self.client.get("/api/email-settings")
        self.assertTrue(response.json()["credential_saved"])
        self.assertTrue(response.json()["credential_persisted"])

    def test_forget_clears_persisted_and_session_credentials(self):
        self.credentials.values["old@example.com"] = "persisted"
        self.service.email_settings._session_credentials["old@example.com"] = "session"
        response = self.client.delete("/api/email-settings/credential")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["credential_saved"])
        self.assertEqual(self.credentials.values, {})
        self.assertEqual(self.service.email_settings._session_credentials, {})

    def test_invalid_email_server_and_port(self):
        for field, value in (
            ("sender", "invalid"), ("receiver", "invalid"),
            ("smtp_server", "bad host!"), ("smtp_port", 70000),
        ):
            body = self.update_body()
            body[field] = value
            with self.subTest(field=field):
                self.assertEqual(self.client.put("/api/email-settings", json=body).status_code, 422)

    def test_no_credential_test_email_failure(self):
        response = self.client.post("/api/email-settings/test")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "No App Password is configured.")

    def test_successful_test_email_uses_existing_sender(self):
        self.credentials.values["old@example.com"] = "test-secret"
        observed = {}

        def send(config, subject, body):
            observed["password"] = os.environ.get("SMTP_PASSWORD")
            observed["subject"] = subject
            return True

        self.service.email_settings.email_sender = send
        response = self.client.post("/api/email-settings/test")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])
        self.assertEqual(observed["password"], "test-secret")
        self.assertNotIn("test-secret", response.text)
        self.assertNotIn("SMTP_PASSWORD", os.environ)

    def test_smtp_failure_is_safely_sanitized(self):
        self.credentials.values["old@example.com"] = "PRIVATE_SECRET"
        self.service.email_settings.email_sender = Mock(
            side_effect=RuntimeError("PRIVATE_SECRET raw smtp error"))
        response = self.client.post("/api/email-settings/test")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("PRIVATE_SECRET", response.text)
        self.assertNotIn("raw smtp error", response.text)

    def test_engine_start_injects_child_only_password(self):
        self.credentials.values["old@example.com"] = "child-secret"
        process = Mock(pid=12345)
        process.poll.return_value = None
        self.launcher.return_value = process
        metadata = {"pid": 12345, "instance_id": "a" * 32}
        self.manager.inspect_status = Mock(side_effect=[
            ProcessStatus("STOPPED"), ProcessStatus("STARTING", metadata)])
        self.service.start()
        environment = self.launcher.call_args.kwargs["env"]
        self.assertEqual(environment["SMTP_PASSWORD"], "child-secret")
        self.assertNotEqual(os.environ.get("SMTP_PASSWORD"), "child-secret")

    def test_email_mutation_and_test_are_blocked_while_running(self):
        self.manager.acquire()
        self.assertEqual(self.client.put("/api/email-settings",
                         json=self.update_body()).status_code, 409)
        self.assertEqual(self.client.post("/api/email-settings/test").status_code, 409)
        self.assertEqual(self.client.delete("/api/email-settings/credential").status_code, 409)

    def test_secure_store_unavailable_fails_without_plaintext_fallback(self):
        unavailable = FakeCredentialStore(available=False)
        service = EngineAPIService(
            manager=self.manager, config_loader=lambda: load_config(self.config_path),
            config_path=self.config_path, credential_store=unavailable,
        )
        with TestClient(create_app(service), base_url="http://localhost") as client:
            response = client.put("/api/email-settings", json=self.update_body())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("group ed", response.text)
        self.assertNotIn("password", self.config_path.read_text(encoding="utf-8").lower())

    def test_existing_monitoring_jobs_survive_email_update(self):
        folder = self.root / "folder"
        folder.mkdir()
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        raw["tasks"].append({
            "id": "report", "name": "Report", "type": "folder_report", "enabled": True,
            "trigger": {"type": "daily", "hour": 8, "minute": 0},
            "parameters": {"path": str(folder)},
        })
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(self.client.put("/api/email-settings",
                         json=self.update_body()).status_code, 200)
        self.assertEqual(load_config(self.config_path).tasks[0].id, "report")
