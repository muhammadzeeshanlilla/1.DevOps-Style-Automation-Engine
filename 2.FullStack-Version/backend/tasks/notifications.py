"""One synchronous lifecycle email; independent of task action outcomes."""
import os
import re
from datetime import datetime
from types import MappingProxyType
from tasks.email_task import send_email
from utils.logger import get_logger

logger = get_logger()


class LifecycleNotificationService:
    def __init__(self, email_settings, settings):
        self._email = MappingProxyType(dict(email_settings))
        self.settings = settings

    def notify(self, result):
        policy = self.settings
        if (not policy.enabled or result.status == "SKIPPED"
                or (result.status == "SUCCESS" and not policy.notify_on_success)
                or (result.status == "FAILED" and not policy.notify_on_failure)):
            return None
        if result.status not in ("SUCCESS", "FAILED"):
            return None
        # IDs are validated by the loader; never echo arbitrary forged identity/error data.
        identity = result.task_id if re.fullmatch(r"[a-z0-9_-]+", result.task_id) else "[UNAVAILABLE]"
        subject = f"Automation Engine - Task {result.status}"
        body = (f"Task ID: {identity}\nStatus: {result.status}\n"
                f"Completed at: {datetime.now().astimezone().isoformat()}\n")
        if result.status == "FAILED":
            body += "Summary: Task execution failed; see task outcome logs.\n"
        password = os.environ.get("SMTP_PASSWORD")
        if password:
            subject = subject.replace(password, "[REDACTED]")
            body = body.replace(password, "[REDACTED]")
        try:
            accepted = send_email({"email": dict(self._email)}, subject, body) is True
        except Exception:
            logger.error("Lifecycle notification failed; acceptance not confirmed.")
            return False
        if accepted:
            logger.info("Lifecycle notification SMTP acceptance confirmed.")
        else:
            logger.error("Lifecycle notification failed; acceptance not confirmed.")
        return accepted
