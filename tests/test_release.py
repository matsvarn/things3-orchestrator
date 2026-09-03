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
    ("relative_path", "current_command", "stale_command", "error"),
    [
        (
            "README.md",
            'uv tool install "git+https://github.com/matsvarn/'
            'things3-orchestrator.git@v0.9.1"',
            'uv tool install "git+https://github.com/matsvarn/'
            'things3-orchestrator.git@v0.8.0"',
            "v0.8.0",
        ),
        (
            "docs/clients.md",
            "codex plugin marketplace add matsvarn/things3-orchestrator "
            "--ref v0.9.1",
            "codex plugin marketplace add matsvarn/things3-orchestrator "
            "--ref v0.8.0",
            "v0.8.0",
        ),
        (
            "docs/clients.md",
            "codex plugin marketplace add matsvarn/things3-orchestrator "
            "--ref v0.9.1",
            "codex plugin marketplace add matsvarn/things3-orchestrator "
            "--ref=v0.8.0",
            "unsupported Codex marketplace install command",
        ),
    ],
)
def test_metadata_rejects_each_stale_install_tag(
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    current_command: str,
    stale_command: str,
    error: str,
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

    with pytest.raises(SystemExit, match=error):
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
        "guide.md:1: unsupported Codex marketplace install command",
        "guide.md: missing codex install for v0.9.1",
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
    ("inline", "expected"),
    [
        (
            "``uv tool install "
            '"git+https://github.com/matsvarn/'
            'things3-orchestrator.git@v0.8.0"``',
            ["guide.md:1: uv install tag v0.8.0 differs from v0.9.1"],
        ),
        (
            "``uv tool install "
            '"git+https://github.com/matsvarn/'
            'things3-orchestrator.git@v0.8.0" <!-- literal -->``',
            [
                "guide.md:1: unsupported uv install command",
                "guide.md: missing uv install for v0.9.1",
            ],
        ),
    ],
)
def test_commonmark_code_spans_are_checked_before_html_comments(
    inline: str, expected: list[str],
) -> None:
    markdown = f"An old example used {inline}.\n"

    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == expected


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


@pytest.mark.parametrize("opener", ["`", "``", "````"])
def test_unmatched_backtick_runs_do_not_hide_install_commands(opener: str) -> None:
    markdown = (
        f"This unmatched delimiter is literal: {opener}\n"
        'uv tool install "git+https://github.com/matsvarn/'
        'things3-orchestrator.git@v0.8.0"\n'
    )

    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == ["guide.md:2: uv install tag v0.8.0 differs from v0.9.1"]


@pytest.mark.parametrize("opener", [r"\`", r"\``", r"\````"])
def test_escaped_backtick_runs_do_not_hide_install_commands(opener: str) -> None:
    markdown = (
        f"This escaped delimiter is literal: {opener}\n"
        'uv tool install "git+https://github.com/matsvarn/'
        'things3-orchestrator.git@v0.8.0"\n'
    )

    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == ["guide.md:2: uv install tag v0.8.0 differs from v0.9.1"]


def test_visible_install_command_is_checked_beside_inline_code() -> None:
    markdown = (
        'uv tool install "git+https://github.com/matsvarn/'
        'things3-orchestrator.git@v0.8.0" (see `uv`)\n'
    )

    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == [
        "guide.md:1: unsupported uv install command",
        "guide.md: missing uv install for v0.9.1",
    ]


def test_visible_print_config_is_checked_beside_inline_code(
    tmp_path: Path,
) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        "things-orchestrator print-config --client codex (see `--help`)\n"
    )

    assert instruction_errors(tmp_path) == [
        "guide.md:1: unsupported print-config command"
    ]


def test_shell_wrapper_cannot_hide_unsafe_print_config(tmp_path: Path) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        "bash -lc 'things-orchestrator print-config --client codex'\n"
    )

    assert instruction_errors(tmp_path) == [
        "guide.md:1: unsupported print-config command"
    ]


@pytest.mark.parametrize(
    "wrapper",
    [
        "env sh -c 'things-orchestrator print-config --client codex'",
        "bash -xc 'things-orchestrator print-config --client codex'",
    ],
)
def test_shell_wrapper_variants_cannot_hide_unsafe_print_config(
    wrapper: str, tmp_path: Path,
) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(f"{wrapper}\n")

    assert instruction_errors(tmp_path) == [
        "guide.md:1: unsupported print-config command"
    ]


def test_safe_but_wrapped_print_config_is_not_an_approved_command(
    tmp_path: Path,
) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        "env sh -c 'things-orchestrator print-config "
        "--client codex --show-secrets'\n"
    )

    assert instruction_errors(tmp_path) == [
        "guide.md:1: unsupported print-config command"
    ]


def test_pipe_stderr_operator_cannot_lend_flags_to_print_config(
    tmp_path: Path,
) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        "things-orchestrator print-config --client codex "
        "|& echo --show-secrets\n"
    )

    assert instruction_errors(tmp_path) == [
        "guide.md:1: usable client config needs --show-secrets"
    ]


@pytest.mark.parametrize(
    ("required_kind", "wrapper"),
    [
        (
            "uv",
            "sh -c 'uv tool install "
            '"git+https://github.com/matsvarn/'
            'things3-orchestrator.git@v0.8.0"\'',
        ),
        (
            "codex",
            "zsh -lc 'codex plugin marketplace add "
            "matsvarn/things3-orchestrator --ref v0.8.0'",
        ),
    ],
)
def test_shell_wrapper_cannot_hide_stale_install(
    required_kind: str, wrapper: str
) -> None:
    assert install_tag_errors(
        f"{wrapper}\n",
        source=Path("guide.md"),
        version="0.9.1",
        required_kind=required_kind,
    ) == [
        f"guide.md:1: unsupported "
        f"{required_kind if required_kind == 'uv' else 'Codex marketplace'} "
        "install command",
        f"guide.md: missing {required_kind} install for v0.9.1",
    ]


@pytest.mark.parametrize(
    ("required_kind", "wrapper"),
    [
        (
            "uv",
            "env sh -c 'uv tool install "
            '"git+https://github.com/matsvarn/'
            'things3-orchestrator.git@v0.8.0"\'',
        ),
        (
            "codex",
            "env bash -xc 'codex plugin marketplace add "
            "matsvarn/things3-orchestrator --ref v0.8.0'",
        ),
    ],
)
def test_wrapped_install_is_reported_but_never_counts_as_required(
    required_kind: str, wrapper: str
) -> None:
    assert install_tag_errors(
        f"{wrapper}\n",
        source=Path("guide.md"),
        version="0.9.1",
        required_kind=required_kind,
    ) == [
        f"guide.md:1: unsupported "
        f"{required_kind if required_kind == 'uv' else 'Codex marketplace'} "
        "install command",
        f"guide.md: missing {required_kind} install for v0.9.1",
    ]


def test_echoed_install_never_counts_as_required() -> None:
    markdown = (
        "echo uv tool install "
        '"git+https://github.com/matsvarn/'
        'things3-orchestrator.git@v0.9.1"\n'
    )

    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == [
        "guide.md:1: unsupported uv install command",
        "guide.md: missing uv install for v0.9.1",
    ]


@pytest.mark.parametrize(
    ("required_kind", "tag", "command", "label"),
    [
        (
            "uv",
            "v0.8.0",
            "uv --quiet tool install "
            '"git+https://github.com/matsvarn/'
            'things3-orchestrator.git@{tag}"',
            "uv",
        ),
        (
            "uv",
            "v0.9.1",
            "uv --quiet tool install "
            '"git+https://github.com/matsvarn/'
            'things3-orchestrator.git@{tag}"',
            "uv",
        ),
        (
            "codex",
            "v0.8.0",
            "codex -c features.foo=false plugin marketplace add "
            "matsvarn/things3-orchestrator --ref {tag}",
            "Codex marketplace",
        ),
        (
            "codex",
            "v0.9.1",
            "codex -c features.foo=false plugin marketplace add "
            "matsvarn/things3-orchestrator --ref {tag}",
            "Codex marketplace",
        ),
    ],
)
def test_noncanonical_direct_install_never_counts_as_required(
    required_kind: str, tag: str, command: str, label: str
) -> None:
    assert install_tag_errors(
        command.format(tag=tag) + "\n",
        source=Path("guide.md"),
        version="0.9.1",
        required_kind=required_kind,
    ) == [
        f"guide.md:1: unsupported {label} install command",
        f"guide.md: missing {required_kind} install for v0.9.1",
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        "--client caddy --client codex",
        "--client=caddy --client hermes",
        "--client caddy --client=cursor-cloud --show-secrets",
        "--cl codex",
        "--cli hermes",
        "--show-secrets",
    ],
)
def test_print_config_requires_one_exact_client_option(
    arguments: str, tmp_path: Path,
) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(f"things-orchestrator print-config {arguments}\n")

    assert instruction_errors(tmp_path) == [
        "guide.md:1: print-config needs exactly one exact --client"
    ]


@pytest.mark.parametrize(
    ("required_kind", "command", "label"),
    [
        (
            "uv",
            "exec uv tool install "
            '"git+https://github.com/matsvarn/'
            'things3-orchestrator.git@v0.9.1"',
            "uv",
        ),
        (
            "uv",
            "command uv tool install "
            '"git+https://github.com/matsvarn/'
            'things3-orchestrator.git@v0.9.1"',
            "uv",
        ),
        (
            "uv",
            "sudo uv tool install "
            '"git+https://github.com/matsvarn/'
            'things3-orchestrator.git@v0.9.1"',
            "uv",
        ),
        (
            "uv",
            "env -u HOME uv tool install "
            '"git+https://github.com/matsvarn/'
            'things3-orchestrator.git@v0.9.1"',
            "uv",
        ),
        (
            "uv",
            "uv tool install "
            '"git+https://github.com/matsvarn/'
            'things3-orchestrator.git@v0.9.1" --quiet',
            "uv",
        ),
        (
            "uv",
            "uv tool install -- "
            '"git+https://github.com/matsvarn/'
            'things3-orchestrator.git@v0.9.1"',
            "uv",
        ),
        (
            "uv",
            "uv tool install "
            '"git+https://example.com/matsvarn/'
            'things3-orchestrator.git@v0.9.1"',
            "uv",
        ),
        (
            "uv",
            "uv tool install "
            '"git+https://github.com/other/'
            'things3-orchestrator.git@v0.9.1"',
            "uv",
        ),
        (
            "codex",
            "codex plugin marketplace add matsvarn/things3-orchestrator "
            "--ref v0.9.1 --ref v0.9.1",
            "Codex marketplace",
        ),
        (
            "codex",
            "codex plugin marketplace add other/things3-orchestrator "
            "--ref v0.9.1",
            "Codex marketplace",
        ),
    ],
)
def test_only_exact_install_grammar_counts_as_required(
    required_kind: str, command: str, label: str
) -> None:
    assert install_tag_errors(
        command + "\n",
        source=Path("guide.md"),
        version="0.9.1",
        required_kind=required_kind,
    ) == [
        f"guide.md:1: unsupported {label} install command",
        f"guide.md: missing {required_kind} install for v0.9.1",
    ]


@pytest.mark.parametrize(
    ("command", "message"),
    [
        (
            "things-orchestrator print-config --client garbage --show-secrets",
            "unsupported print-config client",
        ),
        (
            "things-orchestrator print-config --client codex "
            "--show-secrets --format json",
            "unsupported print-config command",
        ),
        (
            "things-orchestrator print-config --client codex --show-secrets --",
            "unsupported print-config command",
        ),
        (
            "things-orchestrator print-config --client codex "
            "--show-secrets trailing",
            "unsupported print-config command",
        ),
        (
            "things-orchestrator print-config --client codex "
            "--show-secrets --show-secrets",
            "unsupported print-config command",
        ),
    ],
)
def test_print_config_rejects_noncanonical_arguments(
    command: str, message: str, tmp_path: Path,
) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(command + "\n")

    assert instruction_errors(tmp_path) == [f"guide.md:1: {message}"]


def test_shell_continuation_cannot_split_a_stale_install_target() -> None:
    markdown = r'''```console
uv tool install "git+https://github.com/matsvarn/things3-"\
"orchestrator.git@v0.8.0"
```
'''

    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == ["guide.md:2: uv install tag v0.8.0 differs from v0.9.1"]


def test_indented_shell_continuation_preserves_canonical_install() -> None:
    markdown = r'''```console
uv tool install \
  "git+https://github.com/matsvarn/things3-orchestrator.git@v0.9.1"
```
'''

    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == []


def test_indented_shell_continuation_preserves_safe_print_config(
    tmp_path: Path,
) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        "things-orchestrator print-config --client \\\n"
        "  codex --show-secrets\n"
    )

    assert instruction_errors(tmp_path) == []


def test_visible_install_before_multiline_code_span_is_checked() -> None:
    markdown = (
        'uv tool install "git+https://github.com/matsvarn/'
        'things3-orchestrator.git@v0.8.0" ``notes\n'
        "continued notes``\n"
    )

    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == ["guide.md:1: uv install tag v0.8.0 differs from v0.9.1"]


def test_visible_print_config_before_multiline_code_span_is_checked(
    tmp_path: Path,
) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        "things-orchestrator print-config --client codex ``notes\n"
        "continued notes``\n"
    )

    assert instruction_errors(tmp_path) == [
        "guide.md:1: usable client config needs --show-secrets"
    ]


@pytest.mark.parametrize(
    "markdown",
    [
        'uv tool install "git+https://github.com/matsvarn/'
        'things3-orchestrator.git@v0.9.1" \\\n',
        '```console\nuv tool install "git+https://github.com/matsvarn/'
        'things3-orchestrator.git@v0.9.1" \\\n```\n',
    ],
    ids=["eof", "closing-fence"],
)
def test_incomplete_install_continuation_is_rejected(markdown: str) -> None:
    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == [
        "guide.md:1: unsupported uv install command"
        if not markdown.startswith("```")
        else "guide.md:2: unsupported uv install command",
        "guide.md: missing uv install for v0.9.1",
    ]


@pytest.mark.parametrize(
    "markdown",
    [
        "things-orchestrator print-config --client codex --show-secrets \\\n",
        "```console\n"
        "things-orchestrator print-config --client codex --show-secrets \\\n"
        "```\n",
    ],
    ids=["eof", "closing-fence"],
)
def test_incomplete_print_config_continuation_is_rejected(
    markdown: str, tmp_path: Path
) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(markdown)

    line = 2 if markdown.startswith("```") else 1
    assert instruction_errors(tmp_path) == [
        f"guide.md:{line}: unsupported print-config command"
    ]


@pytest.mark.parametrize(
    ("required_kind", "canonical", "variant", "label"),
    [
        (
            "uv",
            'uv tool install "git+https://github.com/matsvarn/'
            'things3-orchestrator.git@v0.9.1"',
            'uv tool install "git+https://github.com/matsvarn/'
            'Things3-Orchestrator.git@v0.8.0"',
            "uv",
        ),
        (
            "uv",
            'uv tool install "git+https://github.com/matsvarn/'
            'things3-orchestrator.git@v0.9.1"',
            'uv tool install "git+https://github.com/MATSVARN/'
            'THINGS3-ORCHESTRATOR.git@v0.8.0"',
            "uv",
        ),
        (
            "codex",
            "codex plugin marketplace add matsvarn/things3-orchestrator "
            "--ref v0.9.1",
            "codex plugin marketplace add matsvarn/Things3-Orchestrator "
            "--ref v0.8.0",
            "Codex marketplace",
        ),
    ],
)
def test_case_variant_install_targets_cannot_escape_validation(
    required_kind: str, canonical: str, variant: str, label: str
) -> None:
    assert install_tag_errors(
        f"{canonical}\n{variant}\n",
        source=Path("guide.md"),
        version="0.9.1",
        required_kind=required_kind,
    ) == [f"guide.md:2: unsupported {label} install command"]


def test_single_quoted_backslash_newline_cannot_normalize_an_install() -> None:
    markdown = (
        "uv tool install 'git+https://github.com/matsvarn/"
        "things3-orchestrator.git@v0.9.1\\\n"
        "'\n"
    )

    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == [
        "guide.md:1: unsupported uv install command",
        "guide.md: missing uv install for v0.9.1",
    ]


def test_single_quoted_backslash_newline_cannot_normalize_print_config(
    tmp_path: Path,
) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        "things-orchestrator print-config --client 'codex\\\n"
        "' --show-secrets\n"
    )

    assert instruction_errors(tmp_path) == [
        "guide.md:1: unsupported print-config command"
    ]


def test_variable_executable_cannot_hide_print_config_intent(
    tmp_path: Path,
) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        'CLI=things-orchestrator; "$CLI" print-config --client codex\n'
    )

    assert instruction_errors(tmp_path) == [
        "guide.md:1: unsupported print-config command"
    ]


@pytest.mark.parametrize(
    ("required_kind", "command", "label"),
    [
        (
            "uv",
            "ORG=matsvarn; NAME=things3-orchestrator; "
            'uv tool install "git+https://github.com/$ORG/$NAME.git@v0.8.0"',
            "uv",
        ),
        (
            "uv",
            "UV=uv; $UV tool install "
            '"git+https://github.com/matsvarn/'
            'things3-orchestrator.git@v0.8.0"',
            "uv",
        ),
        (
            "uv",
            'uv tool install "git+https://github.com/matsvarn/'
            'things3%2Dorchestrator.git@v0.8.0"',
            "uv",
        ),
        (
            "codex",
            "ORG=matsvarn; NAME=things3-orchestrator; "
            'codex plugin marketplace add "$ORG/$NAME" --ref v0.8.0',
            "Codex marketplace",
        ),
    ],
)
def test_indirect_install_intent_is_rejected(
    required_kind: str, command: str, label: str
) -> None:
    assert install_tag_errors(
        command + "\n",
        source=Path("guide.md"),
        version="0.9.1",
        required_kind=required_kind,
    ) == [
        f"guide.md:1: unsupported {label} install command",
        f"guide.md: missing {required_kind} install for v0.9.1",
    ]


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
