"""ProSyndic source settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from config import ConfigurationError
from sources.base import SourceAccountConfig


DEFAULT_PAGE_LIMIT = 100


@dataclass(frozen=True)
class ProSyndicAccountConfig:
    """Runtime settings for one ProSyndic account."""

    name: str
    base_url: str
    username: str
    password: str
    page_limit: int
    runtime_dir: Path
    lock_file: Path


def _variable_prefix(account: SourceAccountConfig) -> str:
    user = account.name[: -len("_prosyndic")]
    return f"DOCUMENT_{user.upper()}_PROSYNDIC_"


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


def _base_url(raw_value: str, variable: str) -> str:
    value = raw_value.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
        raise ConfigurationError(
            f"{variable} must be an https origin such as "
            "https://example.prosyndic-delta.lu."
        )
    return value


def prosyndic_account_from_source(
    account: SourceAccountConfig,
) -> ProSyndicAccountConfig:
    """Validate and return ProSyndic-specific settings."""
    if account.source != "prosyndic":
        raise ConfigurationError(
            f"Account {account.name!r} uses source {account.source!r}, not prosyndic."
        )
    prefix = _variable_prefix(account)
    settings = dict(account.source_settings)
    base_url = settings.get("base_url", "").strip()
    username = settings.get("username", "").strip()
    password = settings.get("password", "")
    if not base_url:
        raise ConfigurationError(f"{prefix}BASE_URL is required for ProSyndic accounts.")
    if not username:
        raise ConfigurationError(f"{prefix}USERNAME is required for ProSyndic accounts.")
    if not password:
        raise ConfigurationError(f"{prefix}PASSWORD is required for ProSyndic accounts.")

    return ProSyndicAccountConfig(
        name=account.name,
        base_url=_base_url(base_url, f"{prefix}BASE_URL"),
        username=username,
        password=password,
        page_limit=_positive_int_setting(
            settings, "page_limit", f"{prefix}PAGE_LIMIT", DEFAULT_PAGE_LIMIT
        ),
        runtime_dir=account.runtime_dir,
        lock_file=account.lock_file,
    )
