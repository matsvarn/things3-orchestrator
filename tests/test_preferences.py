from __future__ import annotations

import json
from pathlib import Path

import pytest

from things_orchestrator.config import (
    ConfigError,
    load_note_style,
    load_source_schemes,
    preferences_path,
    save_note_style,
    save_preferences,
    save_source_schemes,
)


def test_missing_preferences_mean_natural_without_creating_a_file(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"

    assert load_note_style(path=path) == "natural"
    assert load_source_schemes(path=path) == ()
    assert not path.exists()


def test_preferences_path_uses_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert preferences_path() == tmp_path / "things-orchestrator/preferences.json"


def test_save_creates_the_versioned_preference_file(tmp_path: Path) -> None:
    path = tmp_path / "new/preferences.json"

    save_note_style("visual", path=path)

    assert json.loads(path.read_text()) == {"version": 2, "note_style": "visual"}
    assert load_note_style(path=path) == "visual"


def test_save_is_private_atomic_and_preserves_unknown_keys(tmp_path: Path) -> None:
    directory = tmp_path / "things-orchestrator"
    directory.mkdir(mode=0o755)
    path = directory / "preferences.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "note_style": "natural",
                "future_setting": {"kept": True},
            }
        )
    )

    saved = save_note_style("visual", path=path)

    assert saved == path
    assert load_note_style(path=path) == "visual"
    assert json.loads(path.read_text())["future_setting"] == {"kept": True}
    assert path.stat().st_mode & 0o777 == 0o600
    assert directory.stat().st_mode & 0o777 == 0o700
    assert list(directory.glob(".preferences.json.*")) == []


def test_source_schemes_are_casefolded_deduplicated_and_replaceable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "preferences.json"
    save_note_style("visual", path=path)

    save_source_schemes(("Obsidian", "x-devonthink-item", "OBSIDIAN"), path=path)

    assert load_note_style(path=path) == "visual"
    assert load_source_schemes(path=path) == ("obsidian", "x-devonthink-item")
    assert json.loads(path.read_text())["source_schemes"] == [
        "obsidian",
        "x-devonthink-item",
    ]

    save_source_schemes((), path=path)
    assert load_source_schemes(path=path) == ()


@pytest.mark.parametrize(
    "scheme",
    (
        "http",
        "HTTPS",
        "file",
        "things",
        "javascript",
        "DATA",
        "vbscript",
        "1bad",
        "has space",
        "bad:",
        "",
    ),
)
def test_source_scheme_rejects_built_in_dangerous_and_invalid_values(
    tmp_path: Path, scheme: str
) -> None:
    path = tmp_path / "preferences.json"
    save_note_style("natural", path=path)
    original = path.read_text()

    with pytest.raises(ConfigError):
        save_source_schemes((scheme,), path=path)

    assert path.read_text() == original


def test_combined_preference_change_is_atomic_when_a_scheme_is_invalid(
    tmp_path: Path,
) -> None:
    path = tmp_path / "preferences.json"
    save_note_style("natural", path=path)
    original = path.read_text()

    with pytest.raises(ConfigError):
        save_preferences(
            note_style="visual", source_schemes=("javascript",), path=path
        )

    assert path.read_text() == original
    assert load_note_style(path=path) == "natural"


@pytest.mark.parametrize(
    "body",
    (
        "not json",
        "[]",
        '{"version": 3, "note_style": "natural"}',
        '{"version": 1, "note_style": "classic"}',
        '{"version": 1}',
        '{"version": 1, "note_style": "natural", "source_schemes": "obsidian"}',
        '{"version": 1, "note_style": "natural", "source_schemes": ["data"]}',
    ),
)
def test_invalid_preferences_raise_without_overwrite(tmp_path: Path, body: str) -> None:
    path = tmp_path / "preferences.json"
    path.write_text(body)

    with pytest.raises(ConfigError):
        load_note_style(path=path)
    with pytest.raises(ConfigError):
        save_note_style("visual", path=path)

    assert path.read_text() == body


def test_non_utf8_preferences_raise_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "preferences.json"
    original = b"\xff\xfe"
    path.write_bytes(original)

    with pytest.raises(ConfigError):
        save_note_style("visual", path=path)

    assert path.read_bytes() == original
