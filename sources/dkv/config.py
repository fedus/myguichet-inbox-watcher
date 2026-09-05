"""DKV/Lalux EasyApp source settings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config import ConfigurationError
from sources.base import SourceAccountConfig


DEFAULT_OTP_TYPE = "SMS"
DEFAULT_OTP_TIMEOUT_SECONDS = 300
DEFAULT_PAGE_LIMIT = 20


@dataclass(frozen=True)
class DkvAccountConfig:
    """Runtime settings for one DKV/Lalux EasyApp account."""

    name: str
    username: str
    password: str
    otp_type: str
    otp_timeout_seconds: int
    page_limit: int
    runtime_dir: Path
    token_file: Path
    lock_file: Path


def _variable_prefix(account: SourceAccountConfig) -> str:
    user = account.name[: -len("_dkv")]
    return f"DOCUMENT_{user.upper()}_DKV_"


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


def dkv_account_from_source(account: SourceAccountConfig) -> DkvAccountConfig:
    """Validate and return DKV/Lalux EasyApp-specific settings."""
    if account.source != "dkv":
        raise ConfigurationError(
            f"Account {account.name!r} uses source {account.source!r}, not dkv."
        )
    prefix = _variable_prefix(account)
    settings = dict(account.source_settings)
    username = settings.get("username", "").strip()
    password = settings.get("password", "")
    if not username:
        raise ConfigurationError(f"{prefix}USERNAME is required for DKV accounts.")
    if not password:
        raise ConfigurationError(f"{prefix}PASSWORD is required for DKV accounts.")

    otp_type = settings.get("otp_type", DEFAULT_OTP_TYPE).strip().upper()
    if not otp_type:
        raise ConfigurationError(f"{prefix}OTP_TYPE cannot be empty.")

    return DkvAccountConfig(
        name=account.name,
        username=username,
        password=password,
        otp_type=otp_type,
        otp_timeout_seconds=_positive_int_setting(
            settings,
            "otp_timeout_seconds",
            f"{prefix}OTP_TIMEOUT_SECONDS",
            DEFAULT_OTP_TIMEOUT_SECONDS,
        ),
        page_limit=_positive_int_setting(
            settings, "page_limit", f"{prefix}PAGE_LIMIT", DEFAULT_PAGE_LIMIT
        ),
        runtime_dir=account.runtime_dir,
        token_file=account.runtime_dir / "dkv_token.json",
        lock_file=account.lock_file,
    )
