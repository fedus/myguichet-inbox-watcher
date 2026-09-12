"""Shared configuration for the document inbox watcher."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import MutableMapping, Protocol

from outputs.base import OutputConfig
from sources.base import SourceAccountConfig

try:
    from dotenv import load_dotenv as _load_dotenv
except ImportError:  # pragma: no cover - used only when dependencies are partial
    _load_dotenv = None


ROOT = Path(__file__).parent
ENV_FILE = ROOT / ".env"
NAME_PATTERN = re.compile(r"[A-Za-z0-9_]+")


class ConfigurationError(RuntimeError):
    """Raised when a local configuration value is invalid."""


class ConfigProvider(Protocol):
    """Source of fully assembled document account configuration."""

    def load(self) -> None:
        """Load configuration backing data before account resolution."""

    def get_source_account(self, name: str) -> SourceAccountConfig:
        """Return one configured source account by account name or user:source."""

    def get_source_accounts(self) -> list[SourceAccountConfig]:
        """Return all configured source accounts."""


def _fallback_load_dotenv(path: Path, environ: MutableMapping[str, str]) -> None:
    """Load a minimal KEY=VALUE .env file without overriding environment."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in environ:
            continue
        try:
            parsed = shlex.split(value, comments=False, posix=True)
        except ValueError:
            parsed = [value.strip()]
        environ[key] = parsed[0] if parsed else ""


def _normalize_env_value(value: str) -> str:
    """Normalize values that may already have been loaded by Docker Compose."""
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        try:
            parsed = shlex.split(stripped, comments=False, posix=True)
        except ValueError:
            return stripped[1:-1]
        return parsed[0] if len(parsed) == 1 else stripped[1:-1]
    return value


def _validate_name(value: str, variable: str, kind: str) -> str:
    result = value.strip().lower()
    if not result or not NAME_PATTERN.fullmatch(result):
        raise ConfigurationError(
            f"{variable} {kind} may contain only letters, numbers, and underscores."
        )
    return result


def _path_value(raw_value: str, default: Path, root: Path) -> Path:
    value = raw_value.strip()
    if not value:
        return default
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
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


def _output_type_defaults_prefix(output_type: str) -> str:
    return f"DOCUMENT_OUTPUT_{output_type.upper()}_"


def _known_source_names() -> set[str]:
    from sources import available_source_names

    return available_source_names()


def get_bool(name: str, default: bool = False) -> bool:
    """Read a conventional true/false environment variable."""
    value = os.environ.get(name, str(default)).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false.")


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


class EnvConfigProvider:
    """Build account configuration from environment variables and an optional .env file."""

    def __init__(
        self,
        environ: MutableMapping[str, str] | None = None,
        root: Path | None = None,
        env_file: Path | None = None,
    ) -> None:
        self.environ = os.environ if environ is None else environ
        self.root = ROOT if root is None else root
        self.env_file = self.root / ".env" if env_file is None else env_file

    def load(self) -> None:
        """Load the optional local .env file without overriding real variables."""
        if self.env_file.exists():
            self.env_file.chmod(0o600)
        if _load_dotenv is not None and self.environ is os.environ:
            _load_dotenv(self.env_file)
        else:
            _fallback_load_dotenv(self.env_file, self.environ)

    def _raw_prefixed_settings(self, prefix: str) -> dict[str, str]:
        return {
            variable[len(prefix) :].lower(): _normalize_env_value(value)
            for variable, value in self.environ.items()
            if variable.startswith(prefix)
        }

    def configured_users(self) -> list[str]:
        """Return configured document users."""
        raw_users = self.environ.get("DOCUMENT_USERS", "").strip()
        if not raw_users:
            raise ConfigurationError("DOCUMENT_USERS is not set.")
        return _parse_name_list(raw_users, "DOCUMENT_USERS", "user")

    def _infer_user_sources(self, user: str) -> list[str]:
        """Infer a user's sources from registered plugin names and env prefixes."""
        sources: list[str] = []
        for source in sorted(_known_source_names()):
            prefix = _account_prefix(user, source)
            if any(variable.startswith(prefix) for variable in self.environ):
                sources.append(source)
        return sources

    def configured_user_sources(self, user: str) -> list[str]:
        """Return sources configured for one user."""
        variable = f"{_user_prefix(user)}SOURCES"
        raw_sources = self.environ.get(variable, "").strip()
        if raw_sources:
            return _parse_name_list(raw_sources, variable, "source")

        sources = self._infer_user_sources(user)
        if not sources:
            raise ConfigurationError(
                f"{variable} is not set and no known source plugin settings were found for user {user!r}."
            )
        return sources

    def _output_configs(self, user: str, source: str) -> tuple[OutputConfig, ...]:
        variable = f"{_account_prefix(user, source)}OUTPUTS"
        raw_specs = self.environ.get(variable, "folder")
        configs: list[OutputConfig] = []
        for output_name, output_type in _parse_output_specs(raw_specs, variable):
            settings = {
                **self._raw_prefixed_settings(
                    _output_type_defaults_prefix(output_type)
                ),
                **self._raw_prefixed_settings(
                    _output_prefix(user, source, output_name)
                ),
            }
            configs.append(
                OutputConfig(
                    name=output_name,
                    type=output_type,
                    settings=settings,
                )
            )
        return tuple(configs)

    def _source_settings(self, user: str, source: str) -> dict[str, str]:
        settings = self._raw_prefixed_settings(_account_prefix(user, source))
        return {
            key: value
            for key, value in settings.items()
            if key not in {"sources", "outputs", "runtime_dir", "max_document_mb"}
            and not key.startswith("output_")
        }

    def _source_account(self, user: str, source: str) -> SourceAccountConfig:
        account_name = _source_account_name(user, source)
        prefix = _account_prefix(user, source)
        settings = self._raw_prefixed_settings(prefix)
        runtime_dir = _path_value(
            settings.get("runtime_dir", ""),
            self.root / "accounts" / account_name,
            self.root,
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
            source_settings=self._source_settings(user, source),
            output_configs=self._output_configs(user, source),
        )

    def get_source_account(self, name: str) -> SourceAccountConfig:
        """Return one configured source account by account name or user:source."""
        text = name.strip().lower()
        if ":" in text:
            raw_user, raw_source = text.split(":", 1)
            user = _validate_name(raw_user, "account", "user")
            source = _validate_name(raw_source, "account", "source")
            return self._source_account(user, source)

        account_name = _validate_name(text, "account", "account")
        for account in self.get_source_accounts():
            if account.name == account_name:
                return account
        raise ConfigurationError(f"Unknown configured account {name!r}.")

    def get_source_accounts(self) -> list[SourceAccountConfig]:
        """Return all configured source accounts."""
        accounts: list[SourceAccountConfig] = []
        for user in self.configured_users():
            for source in self.configured_user_sources(user):
                accounts.append(self._source_account(user, source))
        return accounts


def default_config_provider() -> EnvConfigProvider:
    """Return the default file/environment-backed config provider."""
    return EnvConfigProvider()


def load_environment() -> None:
    """Load the optional local .env file without overriding real environment variables."""
    default_config_provider().load()


def configured_users() -> list[str]:
    """Return configured document users from the default provider."""
    return default_config_provider().configured_users()


def configured_user_sources(user: str) -> list[str]:
    """Return sources configured for one user from the default provider."""
    return default_config_provider().configured_user_sources(user)


def get_source_account(name: str) -> SourceAccountConfig:
    """Return one configured source account by account name or user:source."""
    return default_config_provider().get_source_account(name)


def get_source_accounts() -> list[SourceAccountConfig]:
    """Return all configured source accounts."""
    return default_config_provider().get_source_accounts()
