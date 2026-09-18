# This file sends the email report to the boss.
# It uses Python's smtplib which is a built-in library for sending emails.
# SMTP is the standard protocol for sending emails.

import os                              # Read the SMTP password from the environment
import smtplib                          # Built-in library for email sending
import ssl                             # Verify the SMTP server's identity
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
