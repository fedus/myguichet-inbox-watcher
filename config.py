"""Shared configuration for the MyGuichet inbox watcher.

The legacy single-account variables are still supported. For multiple
accounts, set ``MYGUICHET_ACCOUNTS`` and use per-account variables with the
account name in the middle, for example ``MYGUICHET_ALICE_SPACE_ID``.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv as _load_dotenv
except ImportError:  # pragma: no cover - used only when dependencies are partial
    _load_dotenv = None


ROOT = Path(__file__).parent
ENV_FILE = ROOT / ".env"
DEFAULT_LANGUAGE = "fr"
DEFAULT_ACCOUNT_NAME = "default"
ACCOUNT_NAME_PATTERN = re.compile(r"[A-Za-z0-9_]+")


class ConfigurationError(RuntimeError):
    """Raised when a local configuration value is invalid."""


@dataclass(frozen=True)
class AccountConfig:
    """Runtime settings and private file locations for one MyGuichet account."""

    name: str
    luxtrust_username: str
    luxtrust_password: str
    space_id: str
    language: str
    headless: bool
    login_timeout_seconds: int
    maximum_attachment_mb: int
    download_dir: Path
    runtime_dir: Path
    cookie_file: Path
    state_file: Path
    profile_dir: Path
    lock_file: Path


def _fallback_load_dotenv(path: Path) -> None:
    """Load a minimal KEY=VALUE .env file without overriding environment."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        try:
            parsed = shlex.split(value, comments=False, posix=True)
        except ValueError:
            parsed = [value.strip()]
        os.environ[key] = parsed[0] if parsed else ""


def load_environment() -> None:
    """Load the optional local .env file without overriding real environment variables."""
    if ENV_FILE.exists():
        # Credentials should not become readable by other local users.
        ENV_FILE.chmod(0o600)
    if _load_dotenv is not None:
        _load_dotenv(ENV_FILE)
    else:
        _fallback_load_dotenv(ENV_FILE)


def _path_value(raw_value: str, default: Path) -> Path:
    value = raw_value.strip()
    if not value:
        return default
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path


def _validate_account_name(name: str) -> str:
    account_name = name.strip()
    if not account_name or not ACCOUNT_NAME_PATTERN.fullmatch(account_name):
        raise ConfigurationError(
            "Account names may contain only letters, numbers, and underscores."
        )
    return account_name


def _account_prefix(name: str) -> str:
    return f"MYGUICHET_{name.upper()}_"


def _account_env(name: str, key: str, default: str = "") -> str:
    return os.environ.get(f"{_account_prefix(name)}{key}", default)


def configured_account_names() -> list[str]:
    """Return configured account names, or the legacy default account."""
    raw_accounts = os.environ.get("MYGUICHET_ACCOUNTS", "").strip()
    if not raw_accounts:
        return [DEFAULT_ACCOUNT_NAME]
    names: list[str] = []
    seen: set[str] = set()
    for raw_name in re.split(r"[,\s]+", raw_accounts):
        if not raw_name:
            continue
        name = _validate_account_name(raw_name)
        key = name.lower()
        if key not in seen:
            names.append(name)
            seen.add(key)
    if not names:
        raise ConfigurationError(
            "MYGUICHET_ACCOUNTS does not contain any account names."
        )
    return names


def get_bool(name: str, default: bool = False) -> bool:
    """Read a conventional true/false environment variable."""
    value = os.environ.get(name, str(default)).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false.")


def _get_account_bool(
    account_name: str, key: str, global_name: str, default: bool
) -> bool:
    variable = f"{_account_prefix(account_name)}{key}"
    if variable in os.environ:
        value = os.environ[variable].strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off"}:
            return False
        raise ConfigurationError(f"{variable} must be true or false.")
    return get_bool(global_name, default)


def get_positive_int(name: str, default: int) -> int:
    """Read a positive integer environment variable."""
    value = os.environ.get(name, str(default)).strip()
    try:
        result = int(value)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be a positive integer.") from error
    if result <= 0:
        raise ConfigurationError(f"{name} must be a positive integer.")
    return result


def _get_account_positive_int(
    account_name: str, key: str, global_name: str, default: int
) -> int:
    variable = f"{_account_prefix(account_name)}{key}"
    if variable in os.environ:
        value = os.environ[variable].strip()
        try:
            result = int(value)
        except ValueError as error:
            raise ConfigurationError(
                f"{variable} must be a positive integer."
            ) from error
        if result <= 0:
            raise ConfigurationError(f"{variable} must be a positive integer.")
        return result
    return get_positive_int(global_name, default)


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
        raise ConfigurationError(
            f"{variable} is not set. Each MyGuichet account has its own space ID; "
            "see README for how to find it."
        )
    if not result.isdigit():
        raise ConfigurationError(f"{variable} must contain only digits.")
    return result


def _legacy_account() -> AccountConfig:
    language = _validate_language(
        os.environ.get("MYGUICHET_LANGUAGE", DEFAULT_LANGUAGE), "MYGUICHET_LANGUAGE"
    )
    download_dir = _path_value(
        os.environ.get("MYGUICHET_DOWNLOAD_DIR", ""), ROOT / "downloads"
    )
    return AccountConfig(
        name=DEFAULT_ACCOUNT_NAME,
        luxtrust_username=os.environ.get("LUXTRUST_USERNAME", "").strip(),
        luxtrust_password=os.environ.get("LUXTRUST_PASSWORD", ""),
        space_id=_validate_space_id(
            os.environ.get("MYGUICHET_SPACE_ID", ""), "MYGUICHET_SPACE_ID"
        ),
        language=language,
        headless=get_bool("MYGUICHET_HEADLESS", default=False),
        login_timeout_seconds=get_positive_int("MYGUICHET_LOGIN_TIMEOUT_SECONDS", 300),
        maximum_attachment_mb=get_positive_int("MYGUICHET_MAX_ATTACHMENT_MB", 100),
        download_dir=download_dir,
        runtime_dir=ROOT,
        cookie_file=ROOT / "cookie.txt",
        state_file=ROOT / "state.json",
        profile_dir=ROOT / ".browser-profile",
        lock_file=ROOT / ".run.lock",
    )


def get_account(name: str) -> AccountConfig:
    """Return one named account configuration."""
    account_name = _validate_account_name(name)
    if (
        account_name == DEFAULT_ACCOUNT_NAME
        and "MYGUICHET_ACCOUNTS" not in os.environ
    ):
        return _legacy_account()

    prefix = _account_prefix(account_name)
    language = _validate_language(
        _account_env(
            account_name,
            "LANGUAGE",
            os.environ.get("MYGUICHET_LANGUAGE", DEFAULT_LANGUAGE),
        ),
        f"{prefix}LANGUAGE",
    )
    runtime_dir = _path_value(
        _account_env(account_name, "RUNTIME_DIR", ""),
        ROOT / "accounts" / account_name,
    )
    return AccountConfig(
        name=account_name,
        luxtrust_username=_account_env(account_name, "LUXTRUST_USERNAME").strip(),
        luxtrust_password=_account_env(account_name, "LUXTRUST_PASSWORD"),
        space_id=_validate_space_id(
            _account_env(account_name, "SPACE_ID"), f"{prefix}SPACE_ID"
        ),
        language=language,
        headless=_get_account_bool(
            account_name, "HEADLESS", "MYGUICHET_HEADLESS", default=False
        ),
        login_timeout_seconds=_get_account_positive_int(
            account_name,
            "LOGIN_TIMEOUT_SECONDS",
            "MYGUICHET_LOGIN_TIMEOUT_SECONDS",
            300,
        ),
        maximum_attachment_mb=_get_account_positive_int(
            account_name,
            "MAX_ATTACHMENT_MB",
            "MYGUICHET_MAX_ATTACHMENT_MB",
            100,
        ),
        download_dir=_path_value(
            _account_env(account_name, "DOWNLOAD_DIR", ""),
            ROOT / "downloads" / account_name,
        ),
        runtime_dir=runtime_dir,
        cookie_file=runtime_dir / "cookie.txt",
        state_file=runtime_dir / "state.json",
        profile_dir=runtime_dir / ".browser-profile",
        lock_file=runtime_dir / ".run.lock",
    )


def get_accounts() -> list[AccountConfig]:
    """Return all configured accounts."""
    return [get_account(name) for name in configured_account_names()]


def get_space_id() -> str:
    """Return the legacy single-account MyGuichet space ID."""
    return _legacy_account().space_id


def get_language() -> str:
    """Return the legacy single-account portal language code."""
    return _legacy_account().language
