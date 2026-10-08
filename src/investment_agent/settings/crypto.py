"""Fernet encryption of secret runtime settings (settings-plan S-D3).

The master key is the bootstrap env ``SETTINGS_ENCRYPTION_KEY`` (a urlsafe
base64 32-byte Fernet key). It is never derived from ``API_KEY`` /
``WEB_SESSION_SECRET``: rotating those must not make stored secrets
unreadable.
"""

from cryptography.fernet import Fernet, InvalidToken

KEY_GENERATOR_COMMAND = (
    'python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"'
)


class SettingsCryptoError(Exception):
    """Base class of the secret-setting encryption errors."""


class EncryptionKeyError(SettingsCryptoError):
    """``SETTINGS_ENCRYPTION_KEY`` is missing or not a valid Fernet key."""


class SecretDecryptError(SettingsCryptoError):
    """A stored secret cannot be decrypted with the configured key."""


def _key_error(reason: str) -> EncryptionKeyError:
    return EncryptionKeyError(
        f"SETTINGS_ENCRYPTION_KEY {reason}; secret settings (LLM / iFinD API keys) "
        f"cannot be stored or read without it. Generate one with: {KEY_GENERATOR_COMMAND} "
        "and set it in the environment (.env) of the api, worker and scheduler; keep it "
        "outside DATA_DIR and keep a copy - stored secrets are lost without it."
    )


class SecretCipher:
    """Encrypts / decrypts secret values with the configured master key."""

    def __init__(self, key: str) -> None:
        if not key or not key.strip():
            raise _key_error("is not set")
        try:
            self._fernet = Fernet(key.strip().encode())
        except (ValueError, TypeError) as exc:
            raise _key_error("is not a valid Fernet key (32 url-safe base64 bytes)") from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise SecretDecryptError(
                "stored secret cannot be decrypted with the current SETTINGS_ENCRYPTION_KEY "
                "(the key changed, or the row is corrupt); restore the previous key or set "
                "the secret again"
            ) from exc


def secret_hint(plaintext: str) -> str:
    """Last 4 characters shown as ``已设置 ···abcd``; nothing for short values."""

    return plaintext[-4:] if len(plaintext) >= 8 else ""
