"""Shared configuration for the document inbox watcher."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from outputs.base import OutputConfig
from sources.base import SourceAccountConfig

try:
    from dotenv import load_dotenv as _load_dotenv
except ImportError:  # pragma: no cover - used only when dependencies are partial
    _load_dotenv = None


ROOT = Path(__file__).parent
ENV_FILE = ROOT / ".env"
DEFAULT_LANGUAGE = "fr"
NAME_PATTERN = re.compile(r"[A-Za-z0-9_]+")


class ConfigurationError(RuntimeError):
    """Raised when a local configuration value is invalid."""


@dataclass(frozen=True)
class AccountConfig:
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
        ENV_FILE.chmod(0o600)
    if _load_dotenv is not None:
        _load_dotenv(ENV_FILE)
    else:
        _fallback_load_dotenv(ENV_FILE)


def _validate_name(value: str, variable: str, kind: str) -> str:
    result = value.strip().lower()
    if not result or not NAME_PATTERN.fullmatch(result):
        raise ConfigurationError(
            f"{variable} {kind} may contain only letters, numbers, and underscores."
        )
    return result


def _path_value(raw_value: str, default: Path) -> Path:
    value = raw_value.strip()
    if not value:
        return default
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path


def _parse_name_list(raw_value: str, variable: str, kind: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for raw_name in re.split(r"[,\s]+", raw_value.strip()):
        if not raw_name:
            continue
        name = _validate_name(raw_name, variable, kind)
        if name not in seen:
            names.append(name)
            seen.add(name)
    if not names:
        raise ConfigurationError(f"{variable} does not contain any {kind} names.")
    return names


def _user_prefix(user: str) -> str:
    return f"DOCUMENT_{user.upper()}_"


def _source_account_name(user: str, source: str) -> str:
    return f"{user}_{source}"


def _account_prefix(user: str, source: str) -> str:
    return f"{_user_prefix(user)}{source.upper()}_"


def _output_prefix(user: str, source: str, output_name: str) -> str:
    return f"{_account_prefix(user, source)}OUTPUT_{output_name.upper()}_"


def _raw_prefixed_settings(prefix: str) -> dict[str, str]:
    return {
        variable[len(prefix) :].lower(): value
        for variable, value in os.environ.items()
        if variable.startswith(prefix)
    }


def configured_users() -> list[str]:
    """Return configured document users."""
    raw_users = os.environ.get("DOCUMENT_USERS", "").strip()
    if not raw_users:
        raise ConfigurationError("DOCUMENT_USERS is not set.")
    return _parse_name_list(raw_users, "DOCUMENT_USERS", "user")


def _known_source_names() -> set[str]:
    from sources import available_source_names

    return available_source_names()


def _infer_user_sources(user: str) -> list[str]:
    """Infer a user's sources from registered plugin names and env prefixes."""
    sources: list[str] = []
    for source in sorted(_known_source_names()):
        prefix = _account_prefix(user, source)
        if any(variable.startswith(prefix) for variable in os.environ):
            sources.append(source)
    return sources


def configured_user_sources(user: str) -> list[str]:
    """Return sources configured for one user."""
    variable = f"{_user_prefix(user)}SOURCES"
    raw_sources = os.environ.get(variable, "").strip()
    if raw_sources:
        return _parse_name_list(raw_sources, variable, "source")

    sources = _infer_user_sources(user)
    if not sources:
        raise ConfigurationError(
            f"{variable} is not set and no known source plugin settings were found for user {user!r}."
        )
    return sources


def get_bool(name: str, default: bool = False) -> bool:
    """Read a conventional true/false environment variable."""
    value = os.environ.get(name, str(default)).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false.")


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


def _positive_int(value: str, variable: str) -> int:
    try:
        result = int(value.strip())
    except ValueError as error:
        raise ConfigurationError(f"{variable} must be a positive integer.") from error
    if result <= 0:
        raise ConfigurationError(f"{variable} must be a positive integer.")
    return result


def _positive_int_setting(
    settings: dict[str, str], key: str, variable: str, default: int
) -> int:
    raw_value = settings.get(key)
    if raw_value is None:
        return default
    return _positive_int(raw_value, variable)


def _octal_mode(raw_value: str, variable: str) -> int:
    try:
        result = int(raw_value.strip(), 8)
    except ValueError as error:
        raise ConfigurationError(
            f"{variable} must be an octal Unix permission mode, such as 0600 or 0644."
        ) from error
    if result < 0 or result > 0o777:
        raise ConfigurationError(
            f"{variable} must be an octal Unix permission mode between 0000 and 0777."
        )
    return result


def _octal_setting(
    settings: dict[str, str], key: str, variable: str, default: int
) -> int:
    raw_value = settings.get(key)
    if raw_value is None:
        return default
    return _octal_mode(raw_value, variable)


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


def _parse_output_specs(raw_value: str, variable: str) -> list[tuple[str, str]]:
    """Parse output specs as either type or name:type."""
    text = raw_value.strip() or "folder"
    outputs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_spec in re.split(r"[,\s]+", text):
        if not raw_spec:
            continue
        parts = raw_spec.split(":")
        if len(parts) == 1:
            output_name = output_type = _validate_name(parts[0], variable, "output")
        elif len(parts) == 2:
            output_name = _validate_name(parts[0], variable, "output name")
            output_type = _validate_name(parts[1], variable, "output type")
        else:
            raise ConfigurationError(
                f"{variable} entries must be output_type or output_name:output_type."
            )
        if output_name in seen:
            raise ConfigurationError(
                f"{variable} contains duplicate output name {output_name!r}."
            )
        outputs.append((output_name, output_type))
        seen.add(output_name)
    return outputs


def _folder_output_config(user: str, source: str, output_name: str) -> OutputConfig:
    prefix = _output_prefix(user, source, output_name)
    settings = _raw_prefixed_settings(prefix)
    account_name = _source_account_name(user, source)
    directory = _path_value(
        settings.get("directory", settings.get("dir", "")),
        ROOT / "downloads" / account_name,
    )
    file_mode = _octal_setting(settings, "file_mode", f"{prefix}FILE_MODE", 0o600)
    dir_mode = _octal_setting(settings, "dir_mode", f"{prefix}DIR_MODE", 0o700)
    return OutputConfig(
        name=output_name,
        type="folder",
        settings={
            "directory": directory,
            "file_mode": file_mode,
            "dir_mode": dir_mode,
        },
    )


def _output_configs(user: str, source: str) -> tuple[OutputConfig, ...]:
    variable = f"{_account_prefix(user, source)}OUTPUTS"
    raw_specs = os.environ.get(variable, "folder")
    configs: list[OutputConfig] = []
    for output_name, output_type in _parse_output_specs(raw_specs, variable):
        if output_type == "folder":
            configs.append(_folder_output_config(user, source, output_name))
        else:
            configs.append(
                OutputConfig(
                    name=output_name,
                    type=output_type,
                    settings=_raw_prefixed_settings(
                        _output_prefix(user, source, output_name)
                    ),
                )
            )
    return tuple(configs)


def _source_settings(user: str, source: str) -> dict[str, str]:
    settings = _raw_prefixed_settings(_account_prefix(user, source))
    return {
        key: value
        for key, value in settings.items()
        if key not in {"sources", "outputs", "runtime_dir", "max_document_mb"}
        and not key.startswith("output_")
    }


def _source_account(user: str, source: str) -> SourceAccountConfig:
    account_name = _source_account_name(user, source)
    prefix = _account_prefix(user, source)
    settings = _raw_prefixed_settings(prefix)
    runtime_dir = _path_value(
        settings.get("runtime_dir", ""),
        ROOT / "accounts" / account_name,
    )
    maximum_document_mb = _positive_int_setting(
        settings, "max_document_mb", f"{prefix}MAX_DOCUMENT_MB", 100
    )
    return SourceAccountConfig(
        name=account_name,
        source=source,
        maximum_document_mb=maximum_document_mb,
        runtime_dir=runtime_dir,
        state_file=runtime_dir / "state.json",
        lock_file=runtime_dir / ".run.lock",
        source_settings=_source_settings(user, source),
        output_configs=_output_configs(user, source),
    )


def get_source_account(name: str) -> SourceAccountConfig:
    """Return one configured source account by account name or user:source."""
    text = name.strip().lower()
    if ":" in text:
        raw_user, raw_source = text.split(":", 1)
        user = _validate_name(raw_user, "account", "user")
        source = _validate_name(raw_source, "account", "source")
        return _source_account(user, source)

    account_name = _validate_name(text, "account", "account")
    for account in get_source_accounts():
        if account.name == account_name:
            return account
    raise ConfigurationError(f"Unknown configured account {name!r}.")


def get_source_accounts() -> list[SourceAccountConfig]:
    """Return all configured source accounts."""
    accounts: list[SourceAccountConfig] = []
    for user in configured_users():
        for source in configured_user_sources(user):
            accounts.append(_source_account(user, source))
    return accounts


def get_myguichet_account(account: SourceAccountConfig) -> AccountConfig:
    """Return MyGuichet-specific settings for a source account."""
    if account.source != "myguichet":
        raise ConfigurationError(
            f"Account {account.name!r} uses source {account.source!r}, not myguichet."
        )
    user = account.name[: -len("_myguichet")]
    prefix = _account_prefix(user, "myguichet")
    settings = dict(account.source_settings)
    language = _validate_language(settings.get("language", DEFAULT_LANGUAGE), f"{prefix}LANGUAGE")
    return AccountConfig(
        name=account.name,
        luxtrust_username=settings.get("luxtrust_username", "").strip(),
        luxtrust_password=settings.get("luxtrust_password", ""),
        space_id=_validate_space_id(settings.get("space_id", ""), f"{prefix}SPACE_ID"),
        language=language,
        headless=_bool_setting(settings, "headless", f"{prefix}HEADLESS", False),
        login_timeout_seconds=_positive_int_setting(
            settings, "login_timeout_seconds", f"{prefix}LOGIN_TIMEOUT_SECONDS", 300
        ),
        runtime_dir=account.runtime_dir,
        cookie_file=account.runtime_dir / "cookie.txt",
        profile_dir=account.runtime_dir / ".browser-profile",
        lock_file=account.lock_file,
    )


def get_space_id() -> str:
    """Return the MyGuichet space ID for the single configured account."""
    accounts = get_source_accounts()
    if len(accounts) != 1:
        raise ConfigurationError("get_space_id() requires exactly one configured account.")
    return get_myguichet_account(accounts[0]).space_id


def get_language() -> str:
    """Return the MyGuichet language for the single configured account."""
    accounts = get_source_accounts()
    if len(accounts) != 1:
        raise ConfigurationError("get_language() requires exactly one configured account.")
    return get_myguichet_account(accounts[0]).language
