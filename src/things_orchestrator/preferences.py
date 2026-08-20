"""Load and change owner preferences without coupling them to credentials."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

NoteStyle = Literal["natural", "visual"]

_CURRENT_VERSION = 1
_DEFAULT_NOTE_STYLE: NoteStyle = "natural"
_NOTE_STYLES = frozenset(("natural", "visual"))
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")
_BUILT_IN_SCHEMES = frozenset(("file", "http", "https", "things"))
_DANGEROUS_SCHEMES = frozenset(("data", "javascript", "vbscript"))


class PreferencesError(ValueError):
    """The saved preferences cannot be used safely."""


@dataclass(frozen=True)
class Preferences:
    """One validated owner preference snapshot."""

    note_style: NoteStyle = "natural"
    source_schemes: tuple[str, ...] = ()


def preferences_path() -> Path:
    """Return the upgrade-stable owner preference path."""
    root = os.environ.get("XDG_CONFIG_HOME")
    base = Path(root) if root else Path.home() / ".config"
    return base / "things-orchestrator" / "preferences.json"


def load_note_style(*, path: Path | None = None) -> NoteStyle:
    """Load the saved style; a missing preference file means natural."""
    return load_preferences(path=path).note_style


def load_source_schemes(*, path: Path | None = None) -> tuple[str, ...]:
    """Load approved third-party app schemes in canonical form."""
    return load_preferences(path=path).source_schemes


def load_preferences(*, path: Path | None = None) -> Preferences:
    """Load one validated preference snapshot."""

    payload = _load(path or preferences_path())
    raw_schemes = payload.get("source_schemes", [])
    assert isinstance(raw_schemes, list)
    return Preferences(
        note_style=cast(
            NoteStyle, payload.get("note_style", _DEFAULT_NOTE_STYLE)
        ),
        source_schemes=_normalize_source_schemes(raw_schemes),
    )


def save_note_style(style: NoteStyle, *, path: Path | None = None) -> Path:
    """Save one style while preserving keys from newer software."""
    return save_preferences(note_style=style, path=path)


def save_source_schemes(
    schemes: Iterable[str], *, path: Path | None = None
) -> Path:
    """Replace the approved third-party source schemes."""
    return save_preferences(source_schemes=schemes, path=path)


def save_preferences(
    *,
    note_style: NoteStyle | None = None,
    source_schemes: Iterable[str] | None = None,
    path: Path | None = None,
) -> Path:
    """Apply one validated preference change atomically."""
    if note_style is None and source_schemes is None:
        raise PreferencesError("No preference change was supplied")
    style = note_style
    if style is not None and style not in _NOTE_STYLES:
        raise PreferencesError(f"Unknown note style: {style}")
    normalized_schemes = (
        _normalize_source_schemes(source_schemes)
        if source_schemes is not None
        else None
    )
    target = path or preferences_path()
    payload = _load(target)
    payload["version"] = _CURRENT_VERSION
    if style is not None:
        payload["note_style"] = style
    elif "note_style" not in payload:
        payload["note_style"] = _DEFAULT_NOTE_STYLE
    if normalized_schemes is not None:
        payload["source_schemes"] = list(normalized_schemes)
    _atomic_write(target, json.dumps(payload, indent=2) + "\n")
    return target


def _normalize_source_schemes(schemes: Iterable[object]) -> tuple[str, ...]:
    if isinstance(schemes, (str, bytes)):
        raise PreferencesError("Source schemes must be a list")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in schemes:
        if not isinstance(raw, str) or not _SCHEME.fullmatch(raw):
            raise PreferencesError(f"Invalid source scheme: {raw!r}")
        scheme = raw.casefold()
        if scheme in _BUILT_IN_SCHEMES:
            raise PreferencesError(f"Source scheme is built in: {scheme}")
        if scheme in _DANGEROUS_SCHEMES:
            raise PreferencesError(f"Source scheme is not allowed: {scheme}")
        if scheme not in seen:
            normalized.append(scheme)
            seen.add(scheme)
    return tuple(normalized)


def _load(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    if not path.is_file():
        raise PreferencesError(f"Preferences path is not a file: {path}")
    try:
        raw: object = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PreferencesError(f"Preferences file is unreadable: {path}") from error
    if not isinstance(raw, dict):
        raise PreferencesError(f"Preferences file must contain a JSON object: {path}")
    payload: dict[str, object] = dict(raw)
    version = payload.get("version")
    if type(version) is not int or version != _CURRENT_VERSION:
        raise PreferencesError(
            f"Preferences version must be {_CURRENT_VERSION}: {path}"
        )
    style = payload.get("note_style")
    if not isinstance(style, str) or style not in _NOTE_STYLES:
        raise PreferencesError(f"Preferences note_style is invalid: {path}")
    if "source_schemes" in payload:
        raw_schemes = payload["source_schemes"]
        if not isinstance(raw_schemes, list):
            raise PreferencesError(
                f"Preferences source_schemes must be a list: {path}"
            )
        _normalize_source_schemes(raw_schemes)
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
