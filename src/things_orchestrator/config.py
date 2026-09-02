"""Private owner configuration for credentials and durable preferences."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

NoteStyle = Literal["natural", "visual"]

_CURRENT_VERSION = 2
_SUPPORTED_VERSIONS = frozenset((1, 2))
_DEFAULT_NOTE_STYLE: NoteStyle = "natural"
_NOTE_STYLES = frozenset(("natural", "visual"))
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")
_BUILT_IN_SCHEMES = frozenset(("file", "http", "https", "things"))
_DANGEROUS_SCHEMES = frozenset(("data", "javascript", "vbscript"))
_LOOPBACK_HOSTS = frozenset(("127.0.0.1", "localhost", "::1"))
_DNS_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


class ConfigError(ValueError):
    """Saved owner configuration cannot be used safely."""


@dataclass(frozen=True, repr=False)
class McpBearer:
    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ConfigError("The MCP bearer is empty")

    def __str__(self) -> str:
        return "<mcp_token>"

    def reveal(self) -> str:
        return self.value


@dataclass(frozen=True)
class McpUrl:
    origin: str

    @property
    def mcp(self) -> str:
        return f"{self.origin}/mcp"

    @property
    def health(self) -> str:
        return f"{self.origin}/health"

    def __str__(self) -> str:
        return self.mcp


@dataclass(frozen=True, repr=False)
class Credentials:
    email: str
    password: str
    bearer: McpBearer | None


@dataclass(frozen=True)
class Preferences:
    note_style: NoteStyle = "natural"
    source_schemes: tuple[str, ...] = ()
    timezone: str | None = None
    mcp_url: McpUrl | None = None


def credentials_path() -> Path:
    return _config_dir() / "credentials.json"


def preferences_path() -> Path:
    return _config_dir() / "preferences.json"


def launcher_path() -> Path:
    root = os.environ.get("XDG_STATE_HOME")
    base = Path(root) if root else Path.home() / ".local" / "state"
    return base / "things-orchestrator" / "launcher"


def _config_dir() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME")
    base = Path(root) if root else Path.home() / ".config"
    return base / "things-orchestrator"


def normalize_mcp_url(raw: str) -> McpUrl:
    value = raw.strip().rstrip("/")
    if not value or "YOUR-HOST" in value.upper():
        raise ConfigError("The MCP URL needs a real host")
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        raise ConfigError("The MCP URL must not contain credentials")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/mcp"}:
        raise ConfigError("The MCP URL needs an origin or an /mcp path")
    host = parsed.hostname
    if host is None or not _valid_network_host(host):
        raise ConfigError("The MCP URL host must be a DNS name or IP address")
    try:
        parsed.port
    except ValueError as error:
        raise ConfigError("The MCP URL port is invalid") from error
    if parsed.scheme == "https" and host:
        pass
    elif parsed.scheme == "http" and host in _LOOPBACK_HOSTS:
        pass
    else:
        raise ConfigError("The MCP URL needs HTTPS or loopback HTTP")
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return McpUrl(origin)


def _valid_network_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        dns = host.removesuffix(".")
        return (
            bool(dns)
            and len(dns) <= 253
            and all(_DNS_LABEL.fullmatch(label) for label in dns.split("."))
        )
    return True


def load_credentials(*, path: Path | None = None) -> Credentials:
    target = path or credentials_path()
    try:
        raw: object = json.loads(target.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigError(
            "Run things-orchestrator login in a private terminal"
        ) from error
    if not isinstance(raw, dict):
        raise ConfigError("Things Cloud credentials were unreadable")
    email = raw.get("email")
    password = raw.get("password")
    token = raw.get("mcp_token")
    if (
        not isinstance(email, str)
        or not email
        or not isinstance(password, str)
        or not password
    ):
        raise ConfigError("Run things-orchestrator login in a private terminal")
    if token is not None and (not isinstance(token, str) or not token):
        raise ConfigError("Things Cloud credentials were unreadable")
    return Credentials(
        email=email,
        password=password,
        bearer=McpBearer(token) if isinstance(token, str) else None,
    )


def save_credentials(
    email: str,
    password: str,
    bearer: McpBearer,
    *,
    path: Path | None = None,
) -> Path:
    target = path or credentials_path()
    payload = (
        json.dumps(
            {"email": email, "password": password, "mcp_token": bearer.reveal()},
            indent=2,
        )
        + "\n"
    )
    _atomic_write(target, payload)
    return target


def save_launcher(executable: Path, *, path: Path | None = None) -> Path:
    resolved = executable.resolve()
    if not resolved.is_absolute() or not resolved.is_file():
        raise ConfigError("The Things launcher must be an existing absolute file")
    if not os.access(resolved, os.X_OK):
        raise ConfigError("The Things launcher must be executable")
    target = path or launcher_path()
    _atomic_write(target, f"{resolved}\n")
    return target


def load_preferences(*, path: Path | None = None) -> Preferences:
    payload = _load_preferences_payload(path or preferences_path())
    raw_schemes = payload.get("source_schemes", [])
    assert isinstance(raw_schemes, list)
    raw_url = payload.get("mcp_url")
    return Preferences(
        note_style=cast(NoteStyle, payload.get("note_style", _DEFAULT_NOTE_STYLE)),
        source_schemes=_normalize_source_schemes(raw_schemes),
        timezone=cast(str | None, payload.get("timezone")),
        mcp_url=normalize_mcp_url(raw_url) if isinstance(raw_url, str) else None,
    )


def load_note_style(*, path: Path | None = None) -> NoteStyle:
    return load_preferences(path=path).note_style


def load_source_schemes(*, path: Path | None = None) -> tuple[str, ...]:
    return load_preferences(path=path).source_schemes


def load_timezone(
    *,
    preferences_file: Path | None = None,
    credentials_file: Path | None = None,
) -> str | None:
    timezone = load_preferences(path=preferences_file).timezone
    if timezone is not None:
        return timezone
    target = credentials_file or credentials_path()
    try:
        raw: object = json.loads(target.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    legacy = raw.get("timezone")
    if not isinstance(legacy, str):
        return None
    return _normalize_timezone(legacy)


def load_mcp_url(
    *,
    preferences_file: Path | None = None,
    credentials_file: Path | None = None,
) -> McpUrl | None:
    del credentials_file
    return load_preferences(path=preferences_file).mcp_url


def load_legacy_mcp_url(*, path: Path) -> McpUrl | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise ConfigError(f"Legacy MCP config path is not a file: {path}")
    try:
        raw: object = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise TypeError
        servers = raw["mcpServers"]
        if not isinstance(servers, dict):
            raise TypeError
        things = servers["things"]
        if not isinstance(things, dict):
            raise TypeError
        url = things["url"]
        if not isinstance(url, str) or not url:
            raise TypeError
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise ConfigError(f"Legacy MCP config is unreadable: {path}") from error
    if "YOUR-HOST" in url.upper():
        return None
    try:
        return normalize_mcp_url(url)
    except ConfigError as error:
        raise ConfigError(f"Legacy MCP config has an invalid URL: {path}") from error


def select_login_mcp_url(
    *,
    explicit: str,
    saved: McpUrl | None,
    legacy: McpUrl | None,
) -> McpUrl:
    if explicit.strip():
        return normalize_mcp_url(explicit)
    return saved or legacy or normalize_mcp_url("http://127.0.0.1:8787")


def save_note_style(style: NoteStyle, *, path: Path | None = None) -> Path:
    return save_preferences(note_style=style, path=path)


def save_source_schemes(schemes: Iterable[str], *, path: Path | None = None) -> Path:
    return save_preferences(source_schemes=schemes, path=path)


def save_preferences(
    *,
    note_style: NoteStyle | None = None,
    source_schemes: Iterable[str] | None = None,
    timezone: str | None = None,
    mcp_url: McpUrl | str | None = None,
    path: Path | None = None,
) -> Path:
    if all(value is None for value in (note_style, source_schemes, timezone, mcp_url)):
        raise ConfigError("No preference change was supplied")
    if note_style is not None and note_style not in _NOTE_STYLES:
        raise ConfigError(f"Unknown note style: {note_style}")
    normalized_schemes = (
        _normalize_source_schemes(source_schemes)
        if source_schemes is not None
        else None
    )
    normalized_timezone = (
        _normalize_timezone(timezone) if timezone is not None else None
    )
    normalized_url = (
        (mcp_url if isinstance(mcp_url, McpUrl) else normalize_mcp_url(mcp_url))
        if mcp_url is not None
        else None
    )
    target = path or preferences_path()
    payload = _load_preferences_payload(target)
    payload["version"] = _CURRENT_VERSION
    if note_style is not None:
        payload["note_style"] = note_style
    elif "note_style" not in payload:
        payload["note_style"] = _DEFAULT_NOTE_STYLE
    if normalized_schemes is not None:
        payload["source_schemes"] = list(normalized_schemes)
    if normalized_timezone is not None:
        payload["timezone"] = normalized_timezone
    if normalized_url is not None:
        payload["mcp_url"] = str(normalized_url)
    _atomic_write(target, json.dumps(payload, indent=2) + "\n")
    return target


def _normalize_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise ConfigError(
            "Timezone needs an IANA name such as Europe/Berlin"
        ) from error
    return value


def _normalize_source_schemes(schemes: Iterable[object]) -> tuple[str, ...]:
    if isinstance(schemes, (str, bytes)):
        raise ConfigError("Source schemes must be a list")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in schemes:
        if not isinstance(raw, str) or not _SCHEME.fullmatch(raw):
            raise ConfigError(f"Invalid source scheme: {raw!r}")
        scheme = raw.casefold()
        if scheme in _BUILT_IN_SCHEMES:
            raise ConfigError(f"Source scheme is built in: {scheme}")
        if scheme in _DANGEROUS_SCHEMES:
            raise ConfigError(f"Source scheme is not allowed: {scheme}")
        if scheme not in seen:
            normalized.append(scheme)
            seen.add(scheme)
    return tuple(normalized)


def _load_preferences_payload(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    if not path.is_file():
        raise ConfigError(f"Preferences path is not a file: {path}")
    try:
        raw: object = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"Preferences file is unreadable: {path}") from error
    if not isinstance(raw, dict):
        raise ConfigError(f"Preferences file must contain a JSON object: {path}")
    payload: dict[str, object] = dict(raw)
    version = payload.get("version")
    if type(version) is not int or version not in _SUPPORTED_VERSIONS:
        raise ConfigError(f"Preferences version is not supported: {path}")
    style = payload.get("note_style")
    if not isinstance(style, str) or style not in _NOTE_STYLES:
        raise ConfigError(f"Preferences note_style is invalid: {path}")
    if "source_schemes" in payload:
        raw_schemes = payload["source_schemes"]
        if not isinstance(raw_schemes, list):
            raise ConfigError(f"Preferences source_schemes must be a list: {path}")
        _normalize_source_schemes(raw_schemes)
    if "timezone" in payload:
        timezone = payload["timezone"]
        if not isinstance(timezone, str):
            raise ConfigError(f"Preferences timezone is invalid: {path}")
        _normalize_timezone(timezone)
    if "mcp_url" in payload:
        raw_url = payload["mcp_url"]
        if not isinstance(raw_url, str):
            raise ConfigError(f"Preferences mcp_url is invalid: {path}")
        normalize_mcp_url(raw_url)
    return payload


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    finally:
        temporary.unlink(missing_ok=True)
