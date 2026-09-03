from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

import scripts.check_release as check_release
from scripts.check_release import (
    archive_skill_mismatches,
    archive_versions,
    install_tag_errors,
    instruction_errors,
    marketplace_errors,
)


def test_archive_versions_read_built_package_metadata(tmp_path: Path) -> None:
    sdist = tmp_path / "package.tar.gz"
    pkg_info = b"Metadata-Version: 2.4\nName: things-orchestrator\nVersion: 0.5.0\n"
    with tarfile.open(sdist, "w:gz") as archive:
        member = tarfile.TarInfo("things_orchestrator-0.5.0/PKG-INFO")
        member.size = len(pkg_info)
        archive.addfile(member, io.BytesIO(pkg_info))

    wheel = tmp_path / "package.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "things_orchestrator-0.5.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: things-orchestrator\nVersion: 0.5.0\n",
        )

    assert archive_versions(sdist, wheel) == ("0.5.0", "0.5.0")


def test_archive_skill_mismatches_reports_missing_and_changed_files(
    tmp_path: Path,
) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("expected\n")
    (skill / "extra.md").write_text("extra\n")
    wheel = tmp_path / "package.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "things_orchestrator/skills/things-orchestrator/SKILL.md", "changed\n"
        )

    assert archive_skill_mismatches(wheel, source=skill) == [
        "changed skill file: SKILL.md",
        "missing skill file: extra.md",
    ]


def test_repository_marketplace_points_at_a_valid_local_plugin(tmp_path: Path) -> None:
    marketplace = tmp_path / ".agents/plugins/marketplace.json"
    marketplace.parent.mkdir(parents=True)
    plugin = tmp_path / "plugin/.codex-plugin"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(
        '{"name":"things-orchestrator","version":"0.9.1"}\n'
    )
    marketplace.write_text(
        """{
  "name": "things-orchestrator",
  "interface": {"displayName": "Things Orchestrator"},
  "plugins": [{
    "name": "things-orchestrator",
    "source": {"source": "local", "path": "./plugin"},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Productivity"
  }]
}
"""
    )

    assert marketplace_errors(tmp_path) == []


def test_repository_marketplace_rejects_paths_outside_the_repository(
    tmp_path: Path,
) -> None:
    marketplace = tmp_path / ".agents/plugins/marketplace.json"
    marketplace.parent.mkdir(parents=True)
    marketplace.write_text(
        """{
  "name": "things-orchestrator",
  "interface": {"displayName": "Things Orchestrator"},
  "plugins": [{
    "name": "things-orchestrator",
    "source": {"source": "local", "path": "./../private-plugin"},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Productivity"
  }]
}
"""
    )

    assert marketplace_errors(tmp_path) == [
        "marketplace plugin things-orchestrator path leaves the repository"
    ]


def test_secret_bearing_client_commands_require_show_secrets(tmp_path: Path) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        """# Connect

things-orchestrator print-config --client codex
things-orchestrator print-config --client hermes
things-orchestrator print-config --client caddy
things-orchestrator print-config --client cursor --show-secrets
things-orchestrator print-config --url https://example.com --client claude-code
things-orchestrator print-config --client=cursor-cloud
"""
    )

    assert instruction_errors(tmp_path) == [
        "guide.md:3: usable client config needs --show-secrets",
        "guide.md:4: usable client config needs --show-secrets",
        "guide.md:7: usable client config needs --show-secrets",
        "guide.md:8: usable client config needs --show-secrets",
    ]


def test_wrapped_client_commands_cannot_evade_secret_checks(tmp_path: Path) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        """# Connect

```console
things-orchestrator print-config \\
  --url https://example.com \\
  --client claude-code
things-orchestrator print-config \\
  --show-secrets \\
  --client cursor
things-orchestrator print-config \\
  --client hermes \\
  --show-secrets
```
"""
    )

    assert instruction_errors(tmp_path) == [
        "guide.md:4: usable client config needs --show-secrets"
    ]


@pytest.mark.parametrize(
    ("relative_path", "current_command", "stale_command"),
    [
        (
            "README.md",
            'uv tool install "git+https://github.com/matsvarn/'
            'things3-orchestrator.git@v0.9.1"',
            'uv tool install "git+https://github.com/matsvarn/'
            'things3-orchestrator.git@v0.8.0"',
        ),
        (
            "docs/clients.md",
            "codex plugin marketplace add matsvarn/things3-orchestrator "
            "--ref v0.9.1",
            "codex plugin marketplace add matsvarn/things3-orchestrator "
            "--ref v0.8.0",
        ),
        (
            "docs/clients.md",
            "codex plugin marketplace add matsvarn/things3-orchestrator "
            "--ref v0.9.1",
            "codex plugin marketplace add matsvarn/things3-orchestrator "
            "--ref=v0.8.0",
        ),
    ],
)
def test_metadata_rejects_each_stale_install_tag(
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    current_command: str,
    stale_command: str,
) -> None:
    target = check_release.ROOT / relative_path
    original_read_text = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        text = original_read_text(path, *args, **kwargs)
        if path == target:
            assert current_command in text
            return text.replace(current_command, f"{current_command}\n{stale_command}")
        return text

    monkeypatch.setattr(Path, "read_text", read_text)

    with pytest.raises(SystemExit, match="v0.8.0"):
        check_release.metadata()


def test_one_marketplace_command_cannot_hide_a_conflicting_ref() -> None:
    errors = install_tag_errors(
        "codex plugin marketplace add matsvarn/things3-orchestrator "
        "--ref v0.9.1 --ref=v0.8.0\n",
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="codex",
    )

    assert errors == [
        "guide.md:1: Codex marketplace ref v0.8.0 differs from v0.9.1"
    ]


def test_non_command_historical_release_tags_are_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = check_release.ROOT / "README.md"
    original_read_text = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        text = original_read_text(path, *args, **kwargs)
        return f"{text}\nVersion v0.8.0 was released earlier.\n" if path == target else text

    monkeypatch.setattr(Path, "read_text", read_text)

    check_release.metadata()


def test_comments_and_later_shell_commands_do_not_supply_show_secrets(
    tmp_path: Path,
) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        """# Connect

```console
things-orchestrator print-config --client codex # --show-secrets
things-orchestrator print-config --client hermes; echo --show-secrets
things-orchestrator print-config --client cursor && echo --show-secrets
```
"""
    )

    assert instruction_errors(tmp_path) == [
        "guide.md:4: usable client config needs --show-secrets",
        "guide.md:5: usable client config needs --show-secrets",
        "guide.md:6: usable client config needs --show-secrets",
    ]


def test_inline_code_client_commands_require_show_secrets(tmp_path: Path) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        "Run `things-orchestrator print-config --client codex` on the host.\n"
    )

    assert instruction_errors(tmp_path) == [
        "guide.md:1: usable client config needs --show-secrets"
    ]


@pytest.mark.parametrize(
    ("relative_path", "current_command", "stale_inline"),
    [
        (
            "README.md",
            'uv tool install "git+https://github.com/matsvarn/'
            'things3-orchestrator.git@v0.9.1"',
            "`uv tool install "
            '"git+https://github.com/matsvarn/'
            'things3-orchestrator.git@v0.8.0"`',
        ),
        (
            "docs/clients.md",
            "codex plugin marketplace add matsvarn/things3-orchestrator "
            "--ref v0.9.1",
            "`codex plugin marketplace add matsvarn/things3-orchestrator "
            "--ref v0.8.0`",
        ),
    ],
)
def test_metadata_rejects_stale_install_tags_in_inline_code(
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    current_command: str,
    stale_inline: str,
) -> None:
    target = check_release.ROOT / relative_path
    original_read_text = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        text = original_read_text(path, *args, **kwargs)
        if path == target:
            assert current_command in text
            return f"{text}\nAn older example used {stale_inline}.\n"
        return text

    monkeypatch.setattr(Path, "read_text", read_text)

    with pytest.raises(SystemExit, match="v0.8.0"):
        check_release.metadata()


def test_shell_command_substitution_inside_a_fence_is_not_markdown_inline_code() -> None:
    markdown = """```console
echo `date`; uv tool install "git+https://github.com/matsvarn/things3-orchestrator.git@v0.8.0"
```
"""

    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == ["guide.md:2: uv install tag v0.8.0 differs from v0.9.1"]


@pytest.mark.parametrize(
    "markdown",
    [
        "<!-- uv tool install "
        '"git+https://github.com/matsvarn/things3-orchestrator.git@v0.9.1" -->\n',
        """<!--
uv tool install "git+https://github.com/matsvarn/things3-orchestrator.git@v0.9.1"
-->
""",
    ],
)
def test_html_comments_cannot_supply_the_required_install(markdown: str) -> None:
    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == ["guide.md: missing uv install for v0.9.1"]


@pytest.mark.parametrize(
    "inline",
    [
        "``uv tool install "
        '"git+https://github.com/matsvarn/things3-orchestrator.git@v0.8.0"``',
        "``uv tool install "
        '"git+https://github.com/matsvarn/things3-orchestrator.git@v0.8.0" '
        "<!-- literal -->``",
    ],
)
def test_commonmark_code_spans_are_checked_before_html_comments(
    inline: str,
) -> None:
    markdown = f"An old example used {inline}.\n"

    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == ["guide.md:1: uv install tag v0.8.0 differs from v0.9.1"]


def test_fence_info_text_is_not_a_valid_closing_fence() -> None:
    markdown = """```console
```not-a-close
<!--
uv tool install "git+https://github.com/matsvarn/things3-orchestrator.git@v0.8.0"
-->
```
"""

    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == ["guide.md:4: uv install tag v0.8.0 differs from v0.9.1"]


@pytest.mark.parametrize("width", [1, 2, 4])
def test_multiline_commonmark_code_spans_are_checked(width: int) -> None:
    delimiter = "`" * width
    markdown = (
        f"An old example used {delimiter}uv tool install\n"
        '"git+https://github.com/matsvarn/'
        f'things3-orchestrator.git@v0.8.0"{delimiter}.\n'
    )

    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == ["guide.md:1: uv install tag v0.8.0 differs from v0.9.1"]


@pytest.mark.parametrize("mixed_closer", ["```~", "~~~`"])
def test_mixed_character_runs_do_not_close_fences(mixed_closer: str) -> None:
    opener = "```console" if mixed_closer.startswith("`") else "~~~console"
    closer = "```" if mixed_closer.startswith("`") else "~~~"
    markdown = f"""{opener}
{mixed_closer}
<!--
uv tool install "git+https://github.com/matsvarn/things3-orchestrator.git@v0.8.0"
-->
{closer}
"""

    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == ["guide.md:4: uv install tag v0.8.0 differs from v0.9.1"]
