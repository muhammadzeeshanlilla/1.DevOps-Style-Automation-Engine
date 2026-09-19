import re

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


EMAIL = re.compile(r"^[^@\s\r\n]+@[^@\s\r\n]+\.[^@\s\r\n]+$")
HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


class EmailSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sender: str = Field(min_length=1, max_length=320)
    receiver: str = Field(min_length=1, max_length=320)
    smtp_server: str = Field(min_length=1, max_length=253)
    smtp_port: int = Field(ge=1, le=65535)
    app_password: SecretStr | None = None
    remember_credential: bool

    @field_validator("sender", "receiver")
    @classmethod
    def valid_email(cls, value):
        value = value.strip()
        if EMAIL.fullmatch(value) is None:
            raise ValueError("invalid email address")
        return value

    @field_validator("smtp_server")
    @classmethod
    def valid_server(cls, value):
        value = value.strip().rstrip(".")
        if HOSTNAME.fullmatch(value) is None:
            raise ValueError("invalid SMTP hostname")
        return value


class EmailSettingsResponse(BaseModel):
    sender: str
    receiver: str
    smtp_server: str
    smtp_port: int
    credential_saved: bool
    credential_persisted: bool
    secure_storage_available: bool


class EmailTestResponse(BaseModel):
    accepted: bool
    message: str


class CredentialDeleteResponse(BaseModel):
    credential_saved: bool = False
    message: str
