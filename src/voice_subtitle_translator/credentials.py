from __future__ import annotations

import keyring

SERVICE_NAME = "voice-subtitle-translator"


class CredentialStore:
    def get(self, provider: str) -> str | None:
        return keyring.get_password(SERVICE_NAME, provider)

    def set(self, provider: str, api_key: str) -> None:
        keyring.set_password(SERVICE_NAME, provider, api_key)

    def delete(self, provider: str) -> None:
        try:
            keyring.delete_password(SERVICE_NAME, provider)
        except keyring.errors.PasswordDeleteError:
            pass

