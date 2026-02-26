from __future__ import annotations

import os
from typing import Literal

KEYCHAIN_SERVICE_ENV = "KEYCHAIN_SERVICE"
DEFAULT_KEYCHAIN_SERVICE = "rentbot"

try:
    import keyring
except Exception:  # pragma: no cover - optional dependency fallback
    keyring = None  # type: ignore[assignment]


SecretSource = Literal["env", "keychain", "default"]


def keychain_service_name() -> str:
    return os.getenv(KEYCHAIN_SERVICE_ENV, DEFAULT_KEYCHAIN_SERVICE).strip() or DEFAULT_KEYCHAIN_SERVICE


def read_secret(
    name: str,
    *,
    required: bool = True,
    default: str | None = None,
) -> str | None:
    value = os.getenv(name)
    if value:
        return value

    if keyring is not None:
        keychain_value = keyring.get_password(keychain_service_name(), name)
        if keychain_value:
            return keychain_value

    if default is not None:
        return default
    if required:
        raise RuntimeError(
            f"Missing secret {name}. Set env var or store it in macOS Keychain "
            f"(service={keychain_service_name()}, account={name})."
        )
    return None


def read_secret_with_source(
    name: str,
    *,
    required: bool = True,
    default: str | None = None,
) -> tuple[str | None, SecretSource]:
    value = os.getenv(name)
    if value:
        return value, "env"

    if keyring is not None:
        keychain_value = keyring.get_password(keychain_service_name(), name)
        if keychain_value:
            return keychain_value, "keychain"

    if default is not None:
        return default, "default"
    if required:
        raise RuntimeError(
            f"Missing secret {name}. Set env var or store it in macOS Keychain "
            f"(service={keychain_service_name()}, account={name})."
        )
    return None, "default"


def write_secret(name: str, value: str) -> None:
    if keyring is None:
        raise RuntimeError("keyring is not installed. Install it with: pip install keyring")
    keyring.set_password(keychain_service_name(), name, value)


def delete_secret(name: str) -> bool:
    if keyring is None:
        raise RuntimeError("keyring is not installed. Install it with: pip install keyring")
    try:
        keyring.delete_password(keychain_service_name(), name)
        return True
    except Exception:
        return False
