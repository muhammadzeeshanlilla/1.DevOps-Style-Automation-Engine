"""Secure email settings orchestration without changing core SMTP behavior."""

import os
from contextlib import contextmanager

from config.loader import ConfigurationError, validate_config
from tasks.email_task import send_email
from api.credential_store import CredentialStoreError, KeyringCredentialStore
from api.schemas.email_settings import (
    CredentialDeleteResponse, EmailSettingsResponse, EmailTestResponse,
)
from api.service import APIError

EMAIL_MUTATION_BLOCKED = "Stop the engine before changing email settings."
TEST_SUBJECT = "DevOps Automation Engine - Test Email"
TEST_BODY = "Your email configuration is working correctly."


@contextmanager
def _smtp_password(secret):
    previous = os.environ.get("SMTP_PASSWORD")
    os.environ["SMTP_PASSWORD"] = secret
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SMTP_PASSWORD", None)
        else:
            os.environ["SMTP_PASSWORD"] = previous


class EmailSettingsService:
    def __init__(self, manager, operation_lock, config_store,
                 credential_store=None, email_sender=None):
        self.manager = manager
        self.operation_lock = operation_lock
        self.config_store = config_store
        self.credential_store = credential_store or KeyringCredentialStore()
        self.email_sender = email_sender or send_email
        self._session_credentials = {}

    @staticmethod
    def _account(sender):
        return sender.strip().casefold()

    @staticmethod
    def _normalize_secret(secret, server):
        value = secret.strip()
        if server.casefold() in {"smtp.gmail.com", "smtp.googlemail.com"}:
            value = "".join(value.split())
        if not value:
            raise APIError(400, "An App Password is required.")
        return value

    def _persistent(self, account, fail=False):
        try:
            return self.credential_store.get(account)
        except CredentialStoreError:
            if fail:
                raise APIError(503, "Secure credential storage is unavailable.") from None
            return None

    def credential(self, sender):
        account = self._account(sender)
        return self._session_credentials.get(account) or self._persistent(account)

    def read(self):
        config = self.config_store._load()
        account = self._account(config.email["sender"])
        session = bool(self._session_credentials.get(account))
        persisted = bool(self._persistent(account))
        return EmailSettingsResponse(
            **dict(config.email),
            credential_saved=session or persisted,
            credential_persisted=persisted,
            secure_storage_available=self.credential_store.available(),
        )

    def update(self, request):
        with self.operation_lock:
            if self.manager.inspect_status().state != "STOPPED":
                raise APIError(409, EMAIL_MUTATION_BLOCKED)
            config = self.config_store._load()
            data = self.config_store.as_json(config)
            previous_account = self._account(config.email["sender"])
            account = self._account(request.sender)
            if previous_account != account and self._persistent(previous_account):
                raise APIError(
                    409, "Forget the saved App Password before changing sender email."
                )
            supplied = request.app_password.get_secret_value() if request.app_password else ""
            secret = (self._normalize_secret(supplied, request.smtp_server)
                      if supplied else self.credential(request.sender))
            if not secret:
                raise APIError(400, "An App Password is required.")
            data["email"] = {
                "sender": request.sender, "receiver": request.receiver,
                "smtp_server": request.smtp_server, "smtp_port": request.smtp_port,
            }
            try:
                validate_config(data)
            except ConfigurationError:
                raise APIError(400, "Email settings are invalid.") from None

            previous_persisted = self._persistent(account, fail=request.remember_credential)
            if request.remember_credential:
                try:
                    self.credential_store.set(account, secret)
                    self.config_store._write(data)
                except APIError:
                    try:
                        if previous_persisted:
                            self.credential_store.set(account, previous_persisted)
                        else:
                            self.credential_store.delete(account)
                    except CredentialStoreError:
                        pass
                    raise
                except CredentialStoreError:
                    raise APIError(503, "App Password could not be stored securely.") from None
                self._session_credentials.pop(account, None)
            else:
                if previous_persisted:
                    try:
                        self.credential_store.delete(account)
                    except CredentialStoreError:
                        raise APIError(503, "Saved App Password could not be removed.") from None
                try:
                    self.config_store._write(data)
                except APIError:
                    if previous_persisted:
                        try:
                            self.credential_store.set(account, previous_persisted)
                        except CredentialStoreError:
                            pass
                    raise
                self._session_credentials[account] = secret
            if previous_account != account:
                self._session_credentials.pop(previous_account, None)
            return self.read()

    def test(self):
        with self.operation_lock:
            if self.manager.inspect_status().state != "STOPPED":
                raise APIError(409, EMAIL_MUTATION_BLOCKED)
            config = self.config_store._load()
            secret = self.credential(config.email["sender"])
            if not secret:
                raise APIError(400, "No App Password is configured.")
            try:
                with _smtp_password(secret):
                    accepted = self.email_sender({"email": dict(config.email)}, TEST_SUBJECT, TEST_BODY) is True
            except Exception:
                accepted = False
            if not accepted:
                raise APIError(503, "SMTP authentication failed or the server could not be reached.") from None
            return EmailTestResponse(
                accepted=True,
                message="SMTP accepted the test email. Check the receiver inbox or spam folder.",
            )

    def forget(self):
        with self.operation_lock:
            if self.manager.inspect_status().state != "STOPPED":
                raise APIError(409, EMAIL_MUTATION_BLOCKED)
            config = self.config_store._load()
            account = self._account(config.email["sender"])
            self._session_credentials.pop(account, None)
            try:
                self.credential_store.delete(account)
            except CredentialStoreError:
                raise APIError(503, "Saved App Password could not be removed.") from None
            return CredentialDeleteResponse(
                message="Saved App Password was removed from this device."
            )
