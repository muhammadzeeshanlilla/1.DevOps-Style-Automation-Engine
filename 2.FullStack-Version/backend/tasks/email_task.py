# This file sends the email report to the boss.
# It uses Python's smtplib which is a built-in library for sending emails.
# SMTP is the standard protocol for sending emails.

import os                              # Read the SMTP password from the environment
import smtplib                          # Built-in library for email sending
import ssl                             # Verify the SMTP server's identity
from datetime import datetime
from pathlib import Path
from email.mime.text import MIMEText    # Used to build the email body
from email.mime.multipart import MIMEMultipart  # Used to build the full email
from utils.logger import get_logger

logger = get_logger()
SMTP_TIMEOUT_SECONDS = 10


def _safe_error(error, password):
    """Keep exception context useful without exposing the SMTP password."""
    messages = []
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        messages.append(str(error).replace(password, "[REDACTED]"))
        error = error.__cause__ or (
            None if error.__suppress_context__ else error.__context__
        )
    return " | caused/preceded by: ".join(messages)

def send_email(config, subject, body):
    """
    This function sends one email.
    config   = the settings loaded from settings.json
    subject  = the email subject line
    body     = the main text inside the email
    """

    password = os.environ.get("SMTP_PASSWORD")
    if not password:
        logger.error("Cannot send email: SMTP_PASSWORD environment variable is missing or empty.")
        return False

    # Report field names only: never dump configuration or invalid values.
    email = config.get("email") if isinstance(config, dict) else None
    if not isinstance(email, dict):
        logger.error("Cannot send email: email configuration section is missing or invalid.")
        return False
    for field in ("sender", "receiver", "smtp_server"):
        value = email.get(field)
        if not isinstance(value, str) or not value.strip() or "\r" in value or "\n" in value:
            logger.error("Cannot send email: invalid email field '%s'.", field)
            return False
    port = email.get("smtp_port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        logger.error("Cannot send email: invalid email field 'smtp_port'.")
        return False
    if not isinstance(subject, str) or "\r" in subject or "\n" in subject:
        logger.error("Cannot send email: invalid Subject header.")
        return False

    sender = email["sender"]
    receiver = email["receiver"]
    server = email["smtp_server"]
    accepted = False

    try:
        # Build the email structure
        msg = MIMEMultipart()
        msg["From"]    = sender
        msg["To"]      = receiver
        msg["Subject"] = subject

        # Attach the body text to the email
        msg.attach(MIMEText(body, "plain"))

        context = ssl.create_default_context()
        with smtplib.SMTP(server, port, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
            smtp.starttls(context=context)    # Require trusted certificate and hostname
            smtp.login(sender, password)       # Login with email and password
            refused = smtp.sendmail(sender, receiver, msg.as_string())
            # smtplib returns an empty dictionary when no recipients were refused.
            accepted = isinstance(refused, dict) and not refused
            if not accepted:
                logger.error("SMTP acceptance was not confirmed for the configured recipient.")

    except Exception as e:
        # If anything goes wrong, log the error but do not crash the engine
        safe_error = _safe_error(e, password)
        if accepted:
            logger.warning("SMTP cleanup failed after confirmed acceptance: %s", safe_error)
        else:
            logger.error("Failed to send email; acceptance not confirmed: %s", safe_error)

    if accepted:
        logger.info((f"SMTP accepted email to {receiver} | Subject: {subject}").replace(password, "[REDACTED]"))
    return accepted


def send_report_email(config, changes):
    """
    This function builds the daily report email from the list of changes
    and calls send_email() to send it.
    changes = list of strings, each describing one file change
    """

    subject = "Automation Engine — Daily Folder Report"

    if not changes:
        # If nothing changed in the folder today, still send an email saying so
        body = "No changes were detected in the watched folder today."
    else:
        # Join all change messages into one readable email body
        body = "The following changes were detected in the watched folder:\n\n"
        body += "\n".join(changes)
        body += "\n\nThis report was generated automatically."

    return send_email(config, subject, body)


def _report_event_line(change):
    prefixes = {
        "NEW file detected: ": "NEW: ",
        "MODIFIED file: ": "MODIFIED: ",
        "DELETED file: ": "DELETED: ",
    }
    return next((label + change[len(prefix):] for prefix, label in prefixes.items()
                 if change.startswith(prefix)), change)


def _folder_label(folder):
    name = Path(folder).name
    return f"...\\{name}" if name else "Configured folder"


def _event_counts(changes):
    return {kind: sum(change.startswith(prefix) for change in changes)
            for kind, prefix in (("new", "NEW file detected: "),
                                 ("modified", "MODIFIED file: "),
                                 ("deleted", "DELETED file: "))}


def send_folder_report_email(config, reports, report_time=None):
    """Build one plain-text message for one or more independently owned batches."""
    reports = tuple(reports)
    if not reports:
        raise ValueError("At least one report is required")
    timestamp = report_time or datetime.now()
    if len(reports) == 1:
        subject = f"DevOps Automation Engine \u2014 Folder Report \u2014 {reports[0]['task_id']}"
        title = "Folder Activity Report"
    else:
        subject = f"DevOps Automation Engine \u2014 {len(reports)} Monitoring Reports \u2014 {timestamp:%H:%M}"
        title = "Consolidated Folder Activity Report"
    lines = ["DevOps Automation Engine", title, "",
             f"Report Time: {timestamp:%Y-%m-%d %H:%M}",
             f"Monitoring Jobs: {len(reports)}"]
    overall = {"new": 0, "modified": 0, "deleted": 0}
    for report in reports:
        changes = report["changes"]
        counts = _event_counts(changes)
        for kind in overall:
            overall[kind] += counts[kind]
        lines.extend(("", "=" * 50,
                      f"Monitoring Job: {report['task_id']}",
                      f"Folder: {_folder_label(report['folder'])}", "", "Changes:"))
        lines.extend((_report_event_line(change) for change in changes)
                     if changes else ("No changes detected.",))
        lines.extend(("", "Summary:", f"New: {counts['new']}",
                      f"Modified: {counts['modified']}", f"Deleted: {counts['deleted']}"))
    if len(reports) > 1:
        lines.extend(("", "=" * 50, "", "Overall Summary:",
                      f"Monitoring Jobs: {len(reports)}",
                      f"New files: {overall['new']}",
                      f"Modified files: {overall['modified']}",
                      f"Deleted files: {overall['deleted']}"))
    lines.extend(("", "This report was generated automatically."))
    return send_email(config, subject, "\n".join(lines))
