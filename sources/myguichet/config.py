"""MyGuichet-specific account settings."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from config import ConfigurationError
from sources.base import SourceAccountConfig


DEFAULT_LANGUAGE = "fr"


@dataclass(frozen=True)
class MyGuichetAccountConfig:
    """Runtime settings for one MyGuichet source account."""

    name: str
    luxtrust_username: str
    luxtrust_password: str
    space_id: str
    language: str
    headless: bool
    login_timeout_seconds: int
    runtime_dir: Path
    cookie_file: Path
    profile_dir: Path
    lock_file: Path


def _variable_prefix(account: SourceAccountConfig) -> str:
    user = account.name[: -len("_myguichet")]
    return f"DOCUMENT_{user.upper()}_MYGUICHET_"


def _bool_setting(settings: dict[str, str], key: str, variable: str, default: bool) -> bool:
    raw_value = settings.get(key)
    if raw_value is None:
        return default
    value = raw_value.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{variable} must be true or false.")


def _positive_int_setting(
    settings: dict[str, str], key: str, variable: str, default: int
) -> int:
    raw_value = settings.get(key)
    if raw_value is None:
        return default
    try:
        result = int(raw_value.strip())
    except ValueError as error:
        raise ConfigurationError(f"{variable} must be a positive integer.") from error
    if result <= 0:
        raise ConfigurationError(f"{variable} must be a positive integer.")
    return result


def _validate_language(language: str, variable: str) -> str:
    result = language.strip().lower()
    if not re.fullmatch(r"[a-z]{2}", result):
        raise ConfigurationError(
            f"{variable} must be a two-letter language code, such as fr or de."
        )
    return result


def _validate_space_id(space_id: str, variable: str) -> str:
    result = space_id.strip()
    if not result:
        raise ConfigurationError(f"{variable} is required for MyGuichet accounts.")
    if not result.isdigit():
        raise ConfigurationError(f"{variable} must contain only digits.")
    return result


def myguichet_account_from_source(
    account: SourceAccountConfig,
) -> MyGuichetAccountConfig:
    """Validate and return MyGuichet-specific settings for a source account."""
    if account.source != "myguichet":
        raise ConfigurationError(
            f"Account {account.name!r} uses source {account.source!r}, not myguichet."
        )
    prefix = _variable_prefix(account)
    settings = dict(account.source_settings)
    language = _validate_language(
        settings.get("language", DEFAULT_LANGUAGE), f"{prefix}LANGUAGE"
    )
    return MyGuichetAccountConfig(
        name=account.name,
        luxtrust_username=settings.get("luxtrust_username", "").strip(),
        luxtrust_password=settings.get("luxtrust_password", ""),
        space_id=_validate_space_id(settings.get("space_id", ""), f"{prefix}SPACE_ID"),
        language=language,
        headless=_bool_setting(settings, "headless", f"{prefix}HEADLESS", True),
        login_timeout_seconds=_positive_int_setting(
            settings, "login_timeout_seconds", f"{prefix}LOGIN_TIMEOUT_SECONDS", 300
        ),
        runtime_dir=account.runtime_dir,
        cookie_file=account.runtime_dir / "cookie.txt",
        profile_dir=account.runtime_dir / ".browser-profile",
        lock_file=account.lock_file,
    )
