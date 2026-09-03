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
    shell_commands,
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


def test_escaped_html_comment_opener_cannot_hide_stale_install() -> None:
    markdown = r'''\<!--
uv tool install "git+https://github.com/matsvarn/things3-orchestrator.git@v0.8.0"
-->
'''

    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == ["guide.md:2: uv install tag v0.8.0 differs from v0.9.1"]


def test_escaped_html_comment_opener_cannot_hide_print_config(
    tmp_path: Path,
) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        "\\<!--\n"
        "things-orchestrator print-config --client codex\n"
        "-->\n"
    )

    assert instruction_errors(tmp_path) == [
        "guide.md:2: usable client config needs --show-secrets"
    ]


def test_variable_things_subcommand_with_client_intent_is_rejected(
    tmp_path: Path,
) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        'things-orchestrator "$ACTION" --client codex --show-secrets\n'
    )

    assert instruction_errors(tmp_path) == [
        "guide.md:1: unsupported print-config command"
    ]


@pytest.mark.parametrize(
    ("required_kind", "command", "label"),
    [
        (
            "uv",
            'uv tool "$ACTION" "git+https://github.com/$ORG/$NAME.git@$TAG"',
            "uv",
        ),
        (
            "codex",
            'codex plugin "$ACTION" "$ORG/$NAME" --ref "$TAG"',
            "Codex marketplace",
        ),
        (
            "codex",
            'codex plugin marketplace "$ACTION" "$ORG/$NAME" --ref "$TAG"',
            "Codex marketplace",
        ),
    ],
)
def test_variable_install_subcommand_is_rejected(
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


def test_metadata_scans_stale_install_in_recovery_guide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = check_release.ROOT / "docs/recovery.md"
    original_read_text = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        text = original_read_text(path, *args, **kwargs)
        if path == target:
            return (
                f'{text}\nuv tool install "git+https://github.com/matsvarn/'
                'things3-orchestrator.git@v0.8.0"\n'
            )
        return text

    monkeypatch.setattr(Path, "read_text", read_text)

    with pytest.raises(SystemExit, match="docs/recovery.md.*v0.8.0"):
        check_release.metadata()


def test_operations_release_template_is_an_exact_exemption() -> None:
    markdown = (
        'uv tool install --force "git+https://github.com/matsvarn/'
        'things3-orchestrator.git@<new-tag>"\n'
    )

    assert install_tag_errors(
        markdown,
        source=Path("docs/operations.md"),
        version="0.9.1",
        required_kind=None,
    ) == []


@pytest.mark.parametrize(
    "hidden",
    [
        "``notes\nstill notes``\n",
        "<!--\nhidden\n-->\n",
    ],
    ids=["multiline-code-span", "html-comment"],
)
def test_hidden_markdown_cannot_complete_install_continuation(
    hidden: str,
) -> None:
    markdown = (
        'uv tool install "git+https://github.com/matsvarn/'
        'things3-orchestrator.git@v0.9.1" \\\n'
        f"{hidden}"
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
    "hidden",
    [
        "``notes\nstill notes``\n",
        "<!--\nhidden\n-->\n",
    ],
    ids=["multiline-code-span", "html-comment"],
)
def test_hidden_markdown_cannot_complete_print_config_continuation(
    hidden: str, tmp_path: Path
) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        "things-orchestrator print-config --client codex --show-secrets \\\n"
        f"{hidden}"
    )

    assert instruction_errors(tmp_path) == [
        "guide.md:1: unsupported print-config command"
    ]


@pytest.mark.parametrize(
    ("markdown", "line"),
    [
        (
            'uv tool install --force "git+https://github.com/matsvarn/'
            'things3-orchestrator.git@<new-tag>" \\\n',
            1,
        ),
        (
            '```console\nuv tool install --force "git+https://github.com/matsvarn/'
            'things3-orchestrator.git@<new-tag>" \\\n```\n',
            2,
        ),
    ],
    ids=["eof", "closing-fence"],
)
def test_incomplete_operations_template_is_not_exempt(
    markdown: str, line: int
) -> None:
    assert install_tag_errors(
        markdown,
        source=Path("docs/operations.md"),
        version="0.9.1",
        required_kind=None,
    ) == [f"docs/operations.md:{line}: unsupported uv install command"]


@pytest.mark.parametrize(
    "markdown",
    [
        'uv tool install \\\n```console\n'
        '"git+https://github.com/matsvarn/'
        'things3-orchestrator.git@v0.9.1"\n```\n',
        'uv tool install "git+https://github.com/matsvarn/'
        'things3-orchestrator.git@v0.9.1" \\\n<!-- note -->\n',
    ],
    ids=["opening-fence", "single-line-comment"],
)
def test_markdown_boundary_cannot_complete_install_continuation(
    markdown: str,
) -> None:
    errors = ["guide.md:1: unsupported uv install command"]
    if markdown.startswith("uv tool install \\"):
        errors.append("guide.md:3: unsupported uv install command")
    errors.append("guide.md: missing uv install for v0.9.1")

    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == errors


@pytest.mark.parametrize(
    "markdown",
    [
        "things-orchestrator print-config --client codex \\\n"
        "```console\n--show-secrets\n```\n",
        "things-orchestrator print-config --client codex --show-secrets \\\n"
        "<!-- note -->\n",
    ],
    ids=["opening-fence", "single-line-comment"],
)
def test_markdown_boundary_cannot_supply_print_config_secret(
    markdown: str, tmp_path: Path
) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(markdown)

    assert instruction_errors(tmp_path) == [
        "guide.md:1: unsupported print-config command"
    ]


@pytest.mark.parametrize(
    "boundary",
    [
        "```console\n\n```\n",
        "<!-- note -->\n",
    ],
    ids=["opening-fence", "single-line-comment"],
)
def test_markdown_boundary_cannot_complete_operations_template(
    boundary: str,
) -> None:
    markdown = (
        'uv tool install --force "git+https://github.com/matsvarn/'
        'things3-orchestrator.git@<new-tag>" \\\n'
        f"{boundary}"
    )

    assert install_tag_errors(
        markdown,
        source=Path("docs/operations.md"),
        version="0.9.1",
        required_kind=None,
    ) == ["docs/operations.md:1: unsupported uv install command"]


def test_visible_install_parts_are_not_joined_across_inline_code() -> None:
    markdown = (
        "uv tool install `NOT-A-SHELL-TOKEN` "
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


def test_print_config_flags_are_not_joined_across_inline_code(
    tmp_path: Path,
) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        "things-orchestrator print-config --client codex "
        "`NOT-A-SHELL-TOKEN` --show-secrets\n"
    )

    assert instruction_errors(tmp_path) == [
        "guide.md:1: usable client config needs --show-secrets"
    ]


@pytest.mark.parametrize(
    "boundary",
    ["\n", "# Heading\n"],
    ids=["blank-line", "atx-heading"],
)
def test_outside_markdown_block_cannot_complete_required_install(
    boundary: str,
) -> None:
    markdown = (
        'uv tool install "git+https://github.com/matsvarn/'
        'things3-orchestrator.git@v0.9.1" \\\n'
        f"{boundary}"
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
    "boundary",
    ["\n", "# Heading\n"],
    ids=["blank-line", "atx-heading"],
)
def test_outside_markdown_block_cannot_complete_print_config(
    boundary: str, tmp_path: Path
) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        "things-orchestrator print-config --client codex --show-secrets \\\n"
        f"{boundary}"
    )

    assert instruction_errors(tmp_path) == [
        "guide.md:1: unsupported print-config command"
    ]


@pytest.mark.parametrize(
    "boundary",
    [
        "- list item",
        "1. ordered item",
        "> quote",
        "---",
    ],
    ids=["unordered-list", "ordered-list", "blockquote", "thematic-break"],
)
def test_other_markdown_block_starts_flush_continuation_incomplete(
    boundary: str,
) -> None:
    commands = shell_commands(f"uv tool install \\\n{boundary}\n")

    assert commands[0].line == 1
    assert commands[0].tokens == ("uv", "tool", "install")
    assert commands[0].complete is False


def test_shell_comment_inside_fence_can_complete_continuation() -> None:
    markdown = (
        "```console\n"
        'uv tool install "git+https://github.com/matsvarn/'
        'things3-orchestrator.git@v0.9.1" \\\n'
        "# shell comment\n"
        "```\n"
    )

    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == []


def test_midword_hash_cannot_turn_invalid_secret_flag_into_comment(
    tmp_path: Path,
) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        "things-orchestrator print-config --client codex "
        "--show-secrets#suffix\n"
    )

    assert instruction_errors(tmp_path) == [
        "guide.md:1: unsupported print-config command"
    ]


@pytest.mark.parametrize(
    ("required_kind", "command", "message"),
    [
        (
            "uv",
            "uv tool install git+https://github.com/matsvarn/"
            "things3-orchestrator.git@v0.9.1#subdirectory=src",
            "uv install tag v0.9.1#subdirectory=src differs from v0.9.1",
        ),
        (
            "codex",
            "codex plugin marketplace add matsvarn/things3-orchestrator "
            "--ref v0.9.1#suffix",
            "Codex marketplace ref v0.9.1#suffix differs from v0.9.1",
        ),
    ],
)
def test_midword_hash_cannot_truncate_install_argument(
    required_kind: str, command: str, message: str
) -> None:
    assert install_tag_errors(
        command + "\n",
        source=Path("guide.md"),
        version="0.9.1",
        required_kind=required_kind,
    ) == [f"guide.md:1: {message}"]


def test_spaced_shell_comments_remain_valid() -> None:
    markdown = (
        'uv tool install "git+https://github.com/matsvarn/'
        'things3-orchestrator.git@v0.9.1" # install comment\n'
        "codex plugin marketplace add matsvarn/things3-orchestrator "
        "--ref v0.9.1 # Codex comment\n"
    )

    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == []
    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="codex",
    ) == []


def test_quoted_hash_remains_part_of_print_config_url(tmp_path: Path) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        "things-orchestrator print-config --client codex "
        "--url 'https://example.com/#fragment' --show-secrets\n"
    )

    assert instruction_errors(tmp_path) == []


def test_midword_hash_does_not_block_shell_continuation() -> None:
    markdown = (
        "uv tool install git+https://github.com/matsvarn/"
        "things3-orchestrator.git@v0.9.1#sub\\\n"
        "directory=src\n"
    )

    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == [
        "guide.md:1: uv install tag v0.9.1#subdirectory=src differs "
        "from v0.9.1"
    ]


def test_real_shell_comment_blocks_trailing_backslash_continuation() -> None:
    markdown = (
        'uv tool install "git+https://github.com/matsvarn/'
        'things3-orchestrator.git@v0.9.1" # comment \\\n'
        "unrelated text\n"
    )

    assert install_tag_errors(
        markdown,
        source=Path("guide.md"),
        version="0.9.1",
        required_kind="uv",
    ) == []


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
