"""Execute configured email content through the existing secure SMTP function."""

from types import MappingProxyType

from tasks.email_task import send_email
from tasks.models import TaskExecutionResult


class EmailHandler:
    def __init__(self, email_settings):
        self._email = MappingProxyType(dict(email_settings))

    def execute(self, task, context=None):
        accepted = send_email({"email": dict(self._email)}, task.parameters["subject"],
                              task.parameters["body"]) is True
        return TaskExecutionResult(
            task.id, "SUCCESS" if accepted else "FAILED",
            error=None if accepted else "SMTP acceptance was not confirmed.",
            notification_accepted=accepted,
        )
