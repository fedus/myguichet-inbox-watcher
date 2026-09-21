"""Foyer source settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config import ConfigurationError
from sources.base import SourceAccountConfig


DEFAULT_PAGE_LIMIT = 50
DEFAULT_LOOKBACK_YEARS = 5


@dataclass(frozen=True)
class FoyerAccountConfig:
    """Runtime settings for one Foyer account."""

    name: str
    username: str
    password: str
    page_limit: int
    lookback_years: int
    runtime_dir: Path
    token_file: Path
    lock_file: Path


def _variable_prefix(account: SourceAccountConfig) -> str:
    user = account.name[: -len("_foyer")]
    return f"DOCUMENT_{user.upper()}_FOYER_"


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


def foyer_account_from_source(account: SourceAccountConfig) -> FoyerAccountConfig:
    """Validate and return Foyer-specific settings."""
    if account.source != "foyer":
        raise ConfigurationError(
            f"Account {account.name!r} uses source {account.source!r}, not foyer."
        )
    prefix = _variable_prefix(account)
    settings = dict(account.source_settings)
    username = settings.get("username", "").strip()
    password = settings.get("password", "")
    if not username:
        raise ConfigurationError(f"{prefix}USERNAME is required for Foyer accounts.")
    if not password:
        raise ConfigurationError(f"{prefix}PASSWORD is required for Foyer accounts.")

    return FoyerAccountConfig(
        name=account.name,
        username=username,
        password=password,
        page_limit=_positive_int_setting(
            settings, "page_limit", f"{prefix}PAGE_LIMIT", DEFAULT_PAGE_LIMIT
        ),
        lookback_years=_positive_int_setting(
            settings,
            "lookback_years",
            f"{prefix}LOOKBACK_YEARS",
            DEFAULT_LOOKBACK_YEARS,
        ),
        runtime_dir=account.runtime_dir,
        token_file=account.runtime_dir / "foyer_token.json",
        lock_file=account.lock_file,
    )
