import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
from api.schemas.responses import OperationResponse
from api.service import APIError
from utils.process_manager import ProcessStatus
from api_tests.helpers import APITestCase


class HTTPTests(APITestCase):
    def test_health_does_not_prepare_engine(self):
        self.loader.side_effect = RuntimeError("private")
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.loader.assert_not_called()
        self.launcher.assert_not_called()
        self.assertFalse(self.manager.lock_path.exists())

    def test_stopped_status(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "STOPPED")
        self.assertFalse(response.json()["runtime"]["available"])

    def test_starting_snapshot(self):
        self.publish("STARTING")
        data = self.client.get("/api/status").json()
        self.assertEqual(data["state"], "STARTING")
        self.assertTrue(data["runtime"]["available"])

    def test_running_snapshot(self):
        self.publish()
        self.runtime.execution_started(self.config.tasks[1])
        self.runtime.publish(self.manager, "RUNNING", "alive", [], 0)
        data = self.client.get("/api/status").json()
        self.assertEqual(data["runtime"]["snapshot"]["current"]["id"], "email-job")
        self.assertNotIn("instance_id", data["runtime"]["snapshot"])
        self.assertNotIn("parameters", json.dumps(data))

    def test_stale_snapshot_is_not_live_status(self):
        path = self.publish()
        data = json.loads(path.read_text())
        data["published_at"] = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        path.write_text(json.dumps(data))
        response = self.client.get("/api/status").json()
        self.assertEqual(response["state"], "RUNNING")
        self.assertFalse(response["runtime"]["available"])
        self.assertNotIn("snapshot", response["runtime"])

    def test_foreign_snapshot_is_suppressed(self):
        path = self.publish()
        data = json.loads(path.read_text())
        data["instance_id"] = "f" * 32
        path.write_text(json.dumps(data))
        self.assertFalse(self.client.get("/api/status").json()["runtime"]["available"])

    def test_corrupt_snapshot_is_suppressed(self):
        self.publish().write_text("private secret invalid JSON")
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("private secret", response.text)

    def test_status_never_returns_raw_metadata_or_details(self):
        self.manager.inspect_status = Mock(return_value=ProcessStatus("UNKNOWN", detail="private path secret"))
        response = self.client.get("/api/status")
        self.assertEqual(response.json()["state"], "UNKNOWN")
        self.assertNotIn("private", response.text)

    def test_ownership_change_discards_snapshot(self):
        self.publish()
        original = self.manager.inspect_status()
        replacement = ProcessStatus("RUNNING", dict(original.metadata, instance_id="f" * 32))
        self.manager.inspect_status = Mock(side_effect=[original, replacement])
        data = self.client.get("/api/status").json()
        self.assertFalse(data["runtime"]["available"])
        self.assertIn("changed", data["runtime"]["reason"])

    def test_tasks_are_frontend_json_without_private_parameters(self):
        response = self.client.get("/api/tasks")
        data = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["tasks"][0]["trigger"], {"type": "daily", "hour": 10, "minute": 30})
        self.assertEqual(data["tasks"][1]["trigger"]["every_minutes"], 5)
        self.assertEqual(data["tasks"][2]["trigger"]["events"], ["new", "modified"])
        for private in ("Private", str(self.root), "smtp.example.com", "private@example.com", "parameters"):
            self.assertNotIn(private, response.text)

    def test_invalid_config_tasks_fail_safely(self):
        self.loader.side_effect = ValueError("private secret config")
        response = self.client.get("/api/tasks")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private secret", response.text)

    def test_password_redacted_from_task_ids(self):
        with patch.dict(os.environ, {"SMTP_PASSWORD": "email-job"}):
            response = self.client.get("/api/tasks")
        self.assertEqual(response.json()["tasks"][1]["id"], "[REDACTED]")
        self.assertNotIn("email-job", response.text)

    def test_missing_logs_are_empty(self):
        data = self.client.get("/api/logs").json()
        self.assertEqual(data["entries"], [])
        self.assertEqual(data["count"], 0)

    def test_logs_limit(self):
        self.log_path.write_text("2026-09-18 12:00:00 [INFO] Task completed | private\n" * 5)
        data = self.client.get("/api/logs?limit=2").json()
        self.assertEqual(data["count"], 2)
        self.assertTrue(data["truncated"])

    def test_invalid_limits_are_rejected(self):
        for limit in ("0", "-1", "201", "not-an-integer"):
            with self.subTest(limit=limit):
                self.assertEqual(self.client.get("/api/logs?limit=" + limit).status_code, 422)

    def test_logs_do_not_leak_paths_bodies_or_credentials(self):
        self.log_path.write_text(
            "2026-09-18 12:00:00 [INFO] SMTP accepted email to private@example.com | Subject: private subject\n"
            "2026-09-18 12:00:01 [ERROR] Failed to send email; acceptance not confirmed: password secret\n"
            "2026-09-18 12:00:02 [INFO] NEW file detected: private-file.txt\n"
            "2026-09-18 12:00:03 [ERROR] body and unknown-token private\n"
            "Traceback: private body and password\n")
        response = self.client.get("/api/logs")
        self.assertEqual(response.json()["count"], 4)
        for value in ("private", "secret", "unknown-token", "Traceback", "@example.com"):
            self.assertNotIn(value, response.text)

    def test_log_read_error_is_safe(self):
        with patch("pathlib.Path.open", side_effect=PermissionError("private secret path")):
            response = self.client.get("/api/logs")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private", response.text)

    def test_start_route_returns_accepted(self):
        self.service.start = Mock(return_value=OperationResponse(state="STARTING", detail="Engine starting."))
        response = self.client.post("/api/engine/start")
        self.assertEqual(response.status_code, 202)
        self.service.start.assert_called_once_with()

    def test_duplicate_start_returns_conflict(self):
        self.service.start = Mock(side_effect=APIError(409, "Engine already owns the runtime."))
        self.assertEqual(self.client.post("/api/engine/start").status_code, 409)

    def test_stop_route_returns_confirmed(self):
        self.service.stop = Mock(return_value=(200, OperationResponse(state="STOPPED", detail="Stopped.")))
        self.assertEqual(self.client.post("/api/engine/stop").status_code, 200)

    def test_stop_route_preserves_unconfirmed_shutdown(self):
        self.service.stop = Mock(return_value=(202, OperationResponse(state="REQUESTED", detail="Not confirmed.")))
        response = self.client.post("/api/engine/stop")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["state"], "REQUESTED")

    def test_vite_cors_preflight(self):
        response = self.client.options("/api/engine/start", headers={
            "Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost:5173")
        self.assertNotIn("access-control-allow-credentials", response.headers)

    def test_unapproved_cors_origin(self):
        response = self.client.options("/api/engine/start", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_cross_site_simple_post_cannot_control_engine(self):
        self.service.start = Mock()
        response = self.client.post("/api/engine/start", headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        self.service.start.assert_not_called()

    def test_vite_origin_can_control_engine(self):
        self.service.start = Mock(return_value=OperationResponse(state="STARTING", detail="Starting."))
        response = self.client.post("/api/engine/start", headers={"Origin": "http://localhost:5173"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost:5173")

    def test_same_origin_swagger_can_control_engine(self):
        self.service.start = Mock(return_value=OperationResponse(state="STARTING", detail="Starting."))
        self.assertEqual(self.client.post("/api/engine/start", headers={"Origin": "http://localhost"}).status_code, 202)

    def test_api_responses_are_not_cached(self):
        self.assertEqual(self.client.get("/api/health").headers["cache-control"], "no-store")

    def test_unexpected_errors_are_not_serialized(self):
        self.service.status = Mock(side_effect=RuntimeError("password secret private path"))
        response = self.client.get("/api/status", headers={"Origin": "http://localhost:5173"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret", response.text)
        self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost:5173")

    def test_validation_errors_do_not_echo_supplied_secret(self):
        response = self.client.get("/api/logs?limit=PRIVATE_SECRET")
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("PRIVATE_SECRET", response.text)

    def test_public_route_contract(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertEqual(set(paths), {"/api/health", "/api/status", "/api/tasks", "/api/logs",
                                     "/api/engine/start", "/api/engine/stop",
                                     "/api/folders/validate", "/api/monitoring-jobs",
                                     "/api/monitoring-jobs/{job_id}",
                                     "/api/monitoring-jobs/{job_id}/enabled",
                                     "/api/email-settings", "/api/email-settings/test",
                                     "/api/email-settings/credential"})
        self.assertEqual(self.client.post("/api/tasks").status_code, 405)
        self.assertEqual(self.client.put("/api/tasks/report").status_code, 404)

    def test_untrusted_host_rejected(self):
        self.assertEqual(self.client.get("/api/health", headers={"Host": "evil.example"}).status_code, 400)

    def test_start_requires_post(self):
        self.assertEqual(self.client.get("/api/engine/start").status_code, 405)
