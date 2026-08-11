"""Shared configuration for the MyGuichet inbox watcher.

Configuration is deliberately kept in one local ``.env`` file so both the
watcher and the browser login use the same settings.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).parent
ENV_FILE = ROOT / ".env"
DEFAULT_LANGUAGE = "fr"


class ConfigurationError(RuntimeError):
    """Raised when a local configuration value is invalid."""


def load_environment() -> None:
    """Load the optional local .env file without overriding real environment variables."""
    if ENV_FILE.exists():
        # Credentials should not become readable by other local users.
        ENV_FILE.chmod(0o600)
    load_dotenv(ENV_FILE)


def get_space_id() -> str:
    """Return the MyGuichet space ID used for API requests.

    Every account has a different space ID, so there is no sensible shared
    default -- each user must set their own in .env. See README for how to
    find it.
    """
    space_id = os.environ.get("MYGUICHET_SPACE_ID", "").strip()
    if not space_id:
        raise ConfigurationError(
            "MYGUICHET_SPACE_ID is not set. Each MyGuichet account has its own space ID; "
            "see README for how to find yours, then set it in .env."
        )
    if not space_id.isdigit():
        raise ConfigurationError("MYGUICHET_SPACE_ID must contain only digits.")
    return space_id


def get_language() -> str:
    """Return a two-letter portal language code."""
    language = os.environ.get("MYGUICHET_LANGUAGE", DEFAULT_LANGUAGE).strip().lower()
    if not re.fullmatch(r"[a-z]{2}", language):
        raise ConfigurationError(
            "MYGUICHET_LANGUAGE must be a two-letter language code, such as fr or de."
        )
    return language


def get_bool(name: str, default: bool = False) -> bool:
    """Read a conventional true/false environment variable."""
    value = os.environ.get(name, str(default)).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false.")


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
