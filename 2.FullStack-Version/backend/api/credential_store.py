"""Small fail-closed adapter around the operating system credential store."""

SERVICE_NAME = "DevOpsAutomationEngine"


class CredentialStoreError(RuntimeError):
    pass


class KeyringCredentialStore:
    def _keyring(self):
        try:
            import keyring
            backend = keyring.get_keyring()
            if float(getattr(backend, "priority", 0)) <= 0:
                raise CredentialStoreError("Secure credential storage is unavailable.")
            return keyring
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreError("Secure credential storage is unavailable.") from None

    def available(self):
        try:
            self._keyring()
            return True
        except CredentialStoreError:
            return False

    def get(self, account):
        try:
            return self._keyring().get_password(SERVICE_NAME, account)
        except Exception:
            raise CredentialStoreError("Secure credential storage is unavailable.") from None

    def set(self, account, secret):
        try:
            self._keyring().set_password(SERVICE_NAME, account, secret)
        except Exception:
            raise CredentialStoreError("App Password could not be stored securely.") from None

    def delete(self, account):
        try:
            keyring = self._keyring()
            if keyring.get_password(SERVICE_NAME, account) is not None:
                keyring.delete_password(SERVICE_NAME, account)
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreError("Saved App Password could not be removed.") from None
