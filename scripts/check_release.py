#!/usr/bin/env python3
"""Check public metadata, local links, and built archive contents."""

from __future__ import annotations

import argparse
import email.parser
import json
import re
import shlex
import subprocess
import tarfile
import tomllib
import urllib.parse
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from things_orchestrator.v2 import MODELS as V2_MODELS

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
GIT_INSTALL_TAG = re.compile(
    r"\Agit\+https://github\.com/matsvarn/things3-orchestrator\.git@"
    r"(?P<tag>v[^\s]+)\Z"
)
ANY_CODEX_TARGET = re.compile(
    r"[^\s'\"]+/things3-orchestrator(?=\s|\Z)", re.IGNORECASE
)
UV_INSTALL_INTENT = re.compile(r"(?:\A|\s)tool\s+install\s+\S")
CODEX_INSTALL_INTENT = re.compile(
    r"(?:\A|\s)plugin\s+marketplace\s+add\s+\S"
)
FENCE_OPEN = re.compile(r"\A {0,3}(?P<marker>`{3,}|~{3,})(?P<info>.*)\Z")
SDIST_FILES = {".gitignore", "LICENSE", "PKG-INFO", "README.md", "pyproject.toml"}
SKILL_ARCHIVE_ROOT = PurePosixPath("things_orchestrator/skills/things-orchestrator")
RETIRED_PUBLIC_TOOLS = ("things_read", "things_commit", "things_approve")


@dataclass(frozen=True, slots=True)
class ShellCommand:
    line: int
    tokens: tuple[str, ...]
    complete: bool = True


def fail(messages: list[str]) -> None:
    if messages:
        raise SystemExit("\n".join(messages))


def metadata() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    manifest = json.loads((ROOT / "plugin/.codex-plugin/plugin.json").read_text())
    errors: list[str] = []
    errors.extend(marketplace_errors())
    errors.extend(product_contract_errors())
    if project["version"] != manifest.get("version"):
        errors.append("pyproject.toml and plugin.json versions differ")

    changelog = (ROOT / "CHANGELOG.md").read_text()
    current_heading = re.search(r"^## ([0-9]+\.[0-9]+\.[0-9]+)\b", changelog, re.MULTILINE)
    if current_heading is None or current_heading.group(1) != project["version"]:
        errors.append("CHANGELOG.md current release differs from pyproject.toml")
    install_guides = {
        ROOT / "README.md": "uv",
        ROOT / "docs/install.md": "uv",
        ROOT / "docs/clients.md": "codex",
    }
    for path in markdown_files():
        errors.extend(
            install_tag_errors(
                path.read_text(),
                source=path.relative_to(ROOT),
                version=project["version"],
                required_kind=install_guides.get(path),
            )
        )

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = next(
        (package for package in lock["package"] if package["name"] == project["name"]),
        None,
    )
    if locked is None or locked["version"] != project["version"]:
        errors.append("uv.lock project version differs from pyproject.toml")

    public_file = ROOT / "public-files.txt"
    public_paths = public_file.read_text().splitlines()
    if public_paths != sorted(set(public_paths)):
        errors.append("public-files.txt must contain unique, sorted paths")
    visible = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    visible_paths = sorted(
        name for name in visible.stdout.splitlines() if (ROOT / name).is_file()
    )
    missing = sorted(set(public_paths) - set(visible_paths))
    extra = sorted(set(visible_paths) - set(public_paths))
    errors.extend(f"public allowlist path is missing: {path}" for path in missing)
    errors.extend(f"public tree path is not allowlisted: {path}" for path in extra)

    skill_dirs = sorted((ROOT / "plugin/skills").iterdir())
    if not skill_dirs:
        errors.append("plugin/skills contains no skill")
    for skill_dir in skill_dirs:
        skill_file = skill_dir / "SKILL.md"
        source = skill_file.read_text() if skill_file.is_file() else ""
        match = re.match(r"\A---\n(.*?)\n---\n", source, re.DOTALL)
        if match is None:
            errors.append(f"{skill_file.relative_to(ROOT)} has invalid frontmatter")
            continue
        frontmatter = yaml.safe_load(match.group(1))
        if frontmatter.get("name") != skill_dir.name:
            errors.append(f"{skill_file.relative_to(ROOT)} name differs from its directory")
        description = frontmatter.get("description")
        if not isinstance(description, str) or not description.strip():
            errors.append(f"{skill_file.relative_to(ROOT)} needs a description")

        agent_file = skill_dir / "agents/openai.yaml"
        agent = yaml.safe_load(agent_file.read_text()) if agent_file.is_file() else {}
        prompt = agent.get("interface", {}).get("default_prompt", "")
        if f"${skill_dir.name}" not in prompt:
            errors.append(f"{agent_file.relative_to(ROOT)} must name ${skill_dir.name}")
        tools = agent.get("dependencies", {}).get("tools", [])
        if not any(tool.get("type") == "mcp" and tool.get("value") == "things" for tool in tools):
            errors.append(f"{agent_file.relative_to(ROOT)} must depend on the things MCP")

        packaged_skill = ROOT / "src/things_orchestrator/skills" / skill_dir.name
        errors.extend(
            f"packaged skill differs: {message}"
            for message in skill_tree_mismatches(packaged_skill, source=skill_dir)
        )

    fail(errors)
    print("Release metadata and skill files are valid.")


def product_contract_errors(root: Path = ROOT) -> list[str]:
    path = root / "PRODUCT.md"
    project_path = root / "pyproject.toml"
    try:
        product = path.read_text()
        version = tomllib.loads(project_path.read_text())["project"]["version"]
    except (OSError, UnicodeError, KeyError, tomllib.TOMLDecodeError):
        return ["PRODUCT.md or pyproject.toml is missing or unreadable"]

    errors: list[str] = []
    if f"Release contract: v{version}" not in product:
        errors.append("PRODUCT.md release contract differs from pyproject.toml")
    if f"exactly {len(V2_MODELS)} bounded tools" not in product:
        errors.append("PRODUCT.md public tool count differs from the executable contract")
    errors.extend(
        f"PRODUCT.md is missing public tool: {tool}"
        for tool in V2_MODELS
        if f"`{tool}`" not in product
    )
    documented_tools = set(re.findall(r"`(things_[a-z_]+)`", product))
    errors.extend(
        f"PRODUCT.md names unknown public tool: {tool}"
        for tool in sorted(
            documented_tools - V2_MODELS.keys() - set(RETIRED_PUBLIC_TOOLS)
        )
    )
    errors.extend(
        f"PRODUCT.md names retired public tool: {tool}"
        for tool in RETIRED_PUBLIC_TOOLS
        if f"`{tool}`" in product
    )
    folded = product.casefold()
    if "unofficial" not in folded or "not affiliated" not in folded:
        errors.append("PRODUCT.md must state the unofficial affiliation boundary")
    if "no read-only bearer" not in folded:
        errors.append("PRODUCT.md must state the shared-bearer authority boundary")
    return errors


def marketplace_errors(root: Path = ROOT) -> list[str]:
    path = root / ".agents/plugins/marketplace.json"
    try:
        payload: object = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ["repository marketplace is missing or unreadable"]
    if not isinstance(payload, dict):
        return ["repository marketplace must contain a JSON object"]

    errors: list[str] = []
    if not isinstance(payload.get("name"), str) or not payload["name"]:
        errors.append("repository marketplace needs a name")
    interface = payload.get("interface")
    if not isinstance(interface, dict) or not isinstance(
        interface.get("displayName"), str
    ):
        errors.append("repository marketplace needs interface.displayName")
    plugins = payload.get("plugins")
    if not isinstance(plugins, list):
        errors.append("repository marketplace plugins must be an array")
        return errors

    names: set[str] = set()
    root_resolved = root.resolve()
    for index, entry in enumerate(plugins):
        if not isinstance(entry, dict):
            errors.append(f"marketplace plugin {index} must be an object")
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            errors.append(f"marketplace plugin {index} needs a name")
            continue
        if name in names:
            errors.append(f"marketplace plugin name is duplicated: {name}")
        names.add(name)

        source = entry.get("source")
        if not isinstance(source, dict) or source.get("source") != "local":
            errors.append(f"marketplace plugin {name} needs a local source")
            continue
        source_path = source.get("path")
        if not isinstance(source_path, str) or not source_path.startswith("./"):
            errors.append(f"marketplace plugin {name} needs a ./ relative path")
            continue
        plugin_root = (root / source_path).resolve()
        if not plugin_root.is_relative_to(root_resolved):
            errors.append(f"marketplace plugin {name} path leaves the repository")
            continue
        manifest_path = plugin_root / ".codex-plugin/plugin.json"
        try:
            manifest: object = json.loads(manifest_path.read_text())
        except (OSError, UnicodeError, json.JSONDecodeError):
            errors.append(f"marketplace plugin {name} manifest is missing or unreadable")
            continue
        if not isinstance(manifest, dict) or manifest.get("name") != name:
            errors.append(f"marketplace plugin {name} differs from its manifest name")

        policy = entry.get("policy")
        if not isinstance(policy, dict):
            errors.append(f"marketplace plugin {name} needs a policy")
        else:
            if policy.get("installation") not in {
                "NOT_AVAILABLE",
                "AVAILABLE",
                "INSTALLED_BY_DEFAULT",
            }:
                errors.append(
                    f"marketplace plugin {name} has an invalid installation policy"
                )
            if policy.get("authentication") not in {"ON_INSTALL", "ON_USE"}:
                errors.append(
                    f"marketplace plugin {name} has an invalid authentication policy"
                )
        if not isinstance(entry.get("category"), str) or not entry["category"]:
            errors.append(f"marketplace plugin {name} needs a category")
    return errors


def markdown_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [ROOT / name for name in result.stdout.splitlines() if (ROOT / name).is_file()]


def links() -> None:
    errors: list[str] = []
    for source in markdown_files():
        for raw in LINK.findall(source.read_text()):
            target = raw.strip().strip("<>").split(maxsplit=1)[0]
            parsed = urllib.parse.urlsplit(target)
            if parsed.scheme or target.startswith("#"):
                continue
            path = urllib.parse.unquote(parsed.path)
            destination = ROOT / path.lstrip("/") if path.startswith("/") else source.parent / path
            if path and not destination.exists():
                errors.append(f"{source.relative_to(ROOT)}: missing local link {target}")
    fail(errors)
    print("Local Markdown links are valid.")


def instruction_errors(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    sources = markdown_files() if root.resolve() == ROOT else sorted(root.rglob("*.md"))
    for source in sources:
        for shell_command in shell_commands(source.read_text()):
            if not _has_print_config_intent(shell_command.tokens):
                continue
            message = (
                _print_config_error(shell_command.tokens)
                if shell_command.complete
                else "unsupported print-config command"
            )
            if message is None:
                continue
            errors.append(
                f"{source.relative_to(root)}:{shell_command.line}: {message}"
            )
    return errors


def install_tag_errors(
    markdown: str,
    *,
    source: Path,
    version: str,
    required_kind: str | None,
) -> list[str]:
    expected = f"v{version}"
    found_required = False
    errors: list[str] = []
    seen_errors: set[str] = set()
    for shell_command in shell_commands(markdown):
        number = shell_command.line
        tokens = shell_command.tokens
        raw = " ".join(tokens)
        if shell_command.complete and _is_operations_release_template(
            tokens, source=source
        ):
            continue
        if (
            _has_uv_install_intent(tokens)
            or (
                not shell_command.complete
                and tokens[:3] == ("uv", "tool", "install")
            )
            or "things3-orchestrator.git" in raw.casefold()
        ):
            tag = (
                _exact_uv_install_tag(
                    tokens, allow_force=source == Path("docs/operations.md")
                )
                if shell_command.complete
                else None
            )
            if tag is None:
                _append_unique(
                    errors,
                    seen_errors,
                    f"{source}:{number}: unsupported uv install command",
                )
            else:
                found_required = found_required or required_kind == "uv"
                if tag != expected:
                    _append_unique(
                        errors,
                        seen_errors,
                        f"{source}:{number}: uv install tag {tag} differs "
                        f"from {expected}",
                    )

        if (
            _has_codex_install_intent(tokens)
            or (
                not shell_command.complete
                and tokens[:4] == ("codex", "plugin", "marketplace", "add")
            )
            or ANY_CODEX_TARGET.search(raw) is not None
        ):
            reference = (
                _exact_codex_install_ref(tokens) if shell_command.complete else None
            )
            if reference is None:
                _append_unique(
                    errors,
                    seen_errors,
                    f"{source}:{number}: unsupported Codex marketplace install command",
                )
            else:
                found_required = found_required or required_kind == "codex"
                if reference != expected:
                    _append_unique(
                        errors,
                        seen_errors,
                        f"{source}:{number}: Codex marketplace ref {reference} "
                        f"differs from {expected}",
                    )
    if required_kind is not None and not found_required:
        errors.append(f"{source}: missing {required_kind} install for {expected}")
    return errors


def shell_commands(markdown: str) -> list[ShellCommand]:
    commands: list[ShellCommand] = []
    fragments: list[str] = []
    start = 1
    fence: tuple[str, int] | None = None
    in_html_comment = False
    code_span: tuple[int, int, list[str]] | None = None
    lines = markdown.splitlines()
    for number, raw_line in enumerate(lines, start=1):
        if fence is not None:
            line = raw_line
            if _closes_fence(raw_line, fence):
                if fragments:
                    logical = "".join(fragments)
                    commands.extend(
                        ShellCommand(start, tokens, complete=False)
                        for tokens in _shell_segments(logical)
                    )
                    fragments = []
                fence = None
                continue
        else:
            if (
                fragments
                and code_span is None
                and not in_html_comment
                and _starts_markdown_block(raw_line)
            ):
                logical = "".join(fragments)
                commands.extend(
                    ShellCommand(start, tokens, complete=False)
                    for tokens in _shell_segments(logical)
                )
                fragments = []
            marker = (
                FENCE_OPEN.match(raw_line)
                if code_span is None and not in_html_comment
                else None
            )
            if marker is not None and not (
                marker.group("marker").startswith("`")
                and "`" in marker.group("info")
            ):
                if fragments:
                    logical = "".join(fragments)
                    commands.extend(
                        ShellCommand(start, tokens, complete=False)
                        for tokens in _shell_segments(logical)
                    )
                    fragments = []
                opening = marker.group("marker")
                fence = (opening[0], len(opening))
                continue
            (
                visible_parts,
                inline,
                in_html_comment,
                code_span,
                crossed_boundary,
            ) = _outside_fence_parts(
                raw_line,
                number=number,
                in_html_comment=in_html_comment,
                code_span=code_span,
                future_lines=lines[number:],
            )
            if crossed_boundary:
                if fragments:
                    fragments.append(visible_parts[0])
                    logical = "".join(fragments)
                    commands.extend(
                        ShellCommand(start, tokens, complete=False)
                        for tokens in _shell_segments(logical)
                    )
                    fragments = []
                    visible_parts = visible_parts[1:]
                for visible in visible_parts:
                    continued = _continues_shell_line(visible)
                    logical = visible[:-1] if continued else visible
                    if not logical.strip():
                        continue
                    commands.extend(
                        ShellCommand(number, tokens, complete=not continued)
                        for tokens in _shell_segments(logical)
                    )
                for code_start, code in inline:
                    commands.extend(
                        ShellCommand(code_start, tokens)
                        for tokens in _shell_segments(code.strip())
                    )
                continue
            line = visible_parts[0]
        if not line.strip() and not fragments:
            continue
        if not fragments:
            start = number
        continued = _continues_shell_line(line)
        fragments.append(line[:-1] if continued else line)
        if continued:
            continue
        logical = "".join(fragments)
        fragments = []
        if not logical.strip():
            continue
        commands.extend(
            ShellCommand(start, tokens) for tokens in _shell_segments(logical)
        )
    if fragments:
        logical = "".join(fragments)
        commands.extend(
            ShellCommand(start, tokens, complete=False)
            for tokens in _shell_segments(logical)
        )
    return commands


def _outside_fence_parts(
    line: str,
    *,
    number: int,
    in_html_comment: bool,
    code_span: tuple[int, int, list[str]] | None,
    future_lines: list[str],
) -> tuple[
    list[str],
    list[tuple[int, str]],
    bool,
    tuple[int, int, list[str]] | None,
    bool,
]:
    visible: list[list[str]] = [[]]
    code: list[tuple[int, str]] = []
    index = 0
    crossed_boundary = code_span is not None or in_html_comment
    if code_span is not None:
        width, start_number, parts = code_span
        closing = _code_span_close(line, 0, width)
        if closing is None:
            parts.append(line)
            return [""], code, in_html_comment, code_span, crossed_boundary
        parts.append(line[:closing])
        code.append((start_number, "\n".join(parts)))
        index = closing + width
        code_span = None
        visible.append([])
    while index < len(line):
        if in_html_comment:
            closing = line.find("-->", index)
            if closing < 0:
                return (
                    ["".join(part) for part in visible],
                    code,
                    True,
                    code_span,
                    crossed_boundary,
                )
            in_html_comment = False
            index = closing + 3
            visible.append([])
            continue
        if line.startswith("<!--", index) and not _is_escaped(line, index):
            crossed_boundary = True
            in_html_comment = True
            index += 4
            continue
        if line[index] == "`" and not _is_escaped(line, index):
            width = 1
            while index + width < len(line) and line[index + width] == "`":
                width += 1
            closing = _code_span_close(line, index + width, width)
            if closing is not None:
                crossed_boundary = True
                code.append((number, line[index + width : closing]))
                index = closing + width
                visible.append([])
                continue
            if not any(
                _code_span_close(future, 0, width) is not None
                for future in future_lines
            ):
                visible[-1].append("`" * width)
                index += width
                continue
            crossed_boundary = True
            code_span = (width, number, [line[index + width :]])
            break
        visible[-1].append(line[index])
        index += 1
    return (
        ["".join(part) for part in visible],
        code,
        in_html_comment,
        code_span,
        crossed_boundary,
    )


def _is_escaped(line: str, index: int) -> bool:
    backslashes = 0
    index -= 1
    while index >= 0 and line[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def _code_span_close(line: str, start: int, width: int) -> int | None:
    delimiter = "`" * width
    index = start
    while (index := line.find(delimiter, index)) >= 0:
        before_is_tick = index > 0 and line[index - 1] == "`"
        after = index + width
        after_is_tick = after < len(line) and line[after] == "`"
        if not before_is_tick and not after_is_tick:
            return index
        index = after
    return None


def _closes_fence(line: str, fence: tuple[str, int]) -> bool:
    character, minimum = fence
    match = re.fullmatch(r" {0,3}(`+|~+)[ \t]*", line)
    if match is None:
        return False
    marker = match.group(1)
    return marker[0] == character and len(marker) >= minimum


def _starts_markdown_block(line: str) -> bool:
    if not line.strip():
        return True
    if re.match(r" {0,3}#{1,6}(?:[ \t]+|$)", line) is not None:
        return True
    if re.match(r" {0,3}>", line) is not None:
        return True
    if re.match(r" {0,3}(?:[*+-]|\d{1,9}[.)])(?:[ \t]+|$)", line) is not None:
        return True
    thematic = line.lstrip(" ") if len(line) - len(line.lstrip(" ")) <= 3 else ""
    compact = thematic.replace(" ", "").replace("\t", "")
    return len(compact) >= 3 and len(set(compact)) == 1 and compact[0] in "*_-"


def _continues_shell_line(line: str) -> bool:
    trailing_backslashes = len(line) - len(line.rstrip("\\"))
    if trailing_backslashes % 2 == 0:
        return False
    if _shell_comment_index(line) is not None:
        return False

    quote: str | None = None
    index = 0
    limit = len(line) - trailing_backslashes
    while index < limit:
        character = line[index]
        if quote == "'":
            if character == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if character == '"':
                quote = None
                index += 1
            elif (
                character == "\\"
                and index + 1 < limit
                and line[index + 1] in '$`"\\'
            ):
                index += 2
            else:
                index += 1
            continue
        if character in {"'", '"'}:
            quote = character
            index += 1
            continue
        if character == "\\" and index + 1 < limit:
            index += 2
            continue
        index += 1
    return quote != "'"


def _shell_segments(command: str) -> list[tuple[str, ...]]:
    comment = _shell_comment_index(command)
    if comment is not None:
        command = command[:comment]
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.commenters = ""
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return [(command,)]
    result: list[tuple[str, ...]] = []
    current: list[str] = []
    for token in tokens:
        if token and set(token) <= {";", "&", "|"}:
            if current:
                result.append(tuple(current))
                current = []
        else:
            current.append(token)
    if current:
        result.append(tuple(current))
    return result


def _shell_comment_index(command: str) -> int | None:
    quote: str | None = None
    word_started = False
    index = 0
    while index < len(command):
        character = command[index]
        if quote == "'":
            if character == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if character == '"':
                quote = None
                index += 1
            elif character == "\\" and index + 1 < len(command):
                index += 2
            else:
                index += 1
            continue
        if character == "#" and not word_started:
            return index
        if character.isspace() or character in ";&|()<>":
            word_started = False
            index += 1
            continue
        if character in {"'", '"'}:
            quote = character
            word_started = True
            index += 1
            continue
        if character == "\\" and index + 1 < len(command):
            word_started = True
            index += 2
            continue
        word_started = True
        index += 1
    return None


def _has_uv_install_intent(tokens: tuple[str, ...]) -> bool:
    if UV_INSTALL_INTENT.search(" ".join(tokens)) is not None:
        return True
    return (
        len(tokens) >= 3
        and tokens[:2] == ("uv", "tool")
        and tokens[2].startswith("$")
    )


def _has_codex_install_intent(tokens: tuple[str, ...]) -> bool:
    if CODEX_INSTALL_INTENT.search(" ".join(tokens)) is not None:
        return True
    return bool(
        len(tokens) >= 3
        and tokens[:2] == ("codex", "plugin")
        and (
            tokens[2].startswith("$")
            or (
                len(tokens) >= 4
                and tokens[2] == "marketplace"
                and tokens[3].startswith("$")
            )
        )
    )


def _has_print_config_intent(tokens: tuple[str, ...]) -> bool:
    raw = " ".join(tokens)
    if "things-orchestrator print-config" in raw:
        return True
    if (
        len(tokens) >= 2
        and tokens[0] == "things-orchestrator"
        and tokens[1].startswith("$")
        and any(
            token == "--client" or token.startswith("--client=")
            for token in tokens[2:]
        )
    ):
        return True
    return any(
        token == "print-config" and index > 0 and tokens[index - 1].startswith("$")
        for index, token in enumerate(tokens)
    )


def _is_operations_release_template(
    tokens: tuple[str, ...], *, source: Path
) -> bool:
    return source == Path("docs/operations.md") and tokens == (
        "uv",
        "tool",
        "install",
        "--force",
        "git+https://github.com/matsvarn/things3-orchestrator.git@<new-tag>",
    )


def _exact_uv_install_tag(
    tokens: tuple[str, ...], *, allow_force: bool
) -> str | None:
    if tokens[:3] != ("uv", "tool", "install"):
        return None
    arguments = tokens[3:]
    if allow_force and len(arguments) == 2 and arguments[0] == "--force":
        target = arguments[1]
    elif len(arguments) == 1:
        target = arguments[0]
    else:
        return None
    match = GIT_INSTALL_TAG.fullmatch(target)
    return match.group("tag") if match is not None else None


def _exact_codex_install_ref(tokens: tuple[str, ...]) -> str | None:
    prefix = (
        "codex",
        "plugin",
        "marketplace",
        "add",
        "matsvarn/things3-orchestrator",
        "--ref",
    )
    if len(tokens) != 7 or tokens[:6] != prefix or not tokens[6].startswith("v"):
        return None
    return tokens[6]


def _print_config_error(tokens: tuple[str, ...]) -> str | None:
    if tokens[:2] != ("things-orchestrator", "print-config"):
        return "unsupported print-config command"
    clients: list[str | None] = []
    show_secrets = 0
    urls = 0
    unsupported = False
    index = 2
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--client="):
            clients.append(token.partition("=")[2] or None)
        elif token == "--client":
            value = tokens[index + 1] if index + 1 < len(tokens) else None
            if value is None or value.startswith("-"):
                clients.append(None)
            else:
                clients.append(value)
                index += 1
        elif token == "--show-secrets":
            show_secrets += 1
        elif token.startswith("--url="):
            urls += 1
            unsupported = unsupported or not token.partition("=")[2]
        elif token == "--url":
            value = tokens[index + 1] if index + 1 < len(tokens) else None
            if value is None or value.startswith("-"):
                unsupported = True
            else:
                urls += 1
                index += 1
        else:
            unsupported = True
        index += 1

    if len(clients) != 1 or clients[0] is None:
        return "print-config needs exactly one exact --client"
    client = clients[0]
    supported = {
        "caddy",
        "claude-code",
        "codex",
        "cursor",
        "cursor-cloud",
        "grok",
        "hermes",
    }
    if client not in supported:
        return "unsupported print-config client"
    if unsupported or show_secrets > 1 or urls > 1:
        return "unsupported print-config command"
    if client != "caddy" and show_secrets != 1:
        return "usable client config needs --show-secrets"
    return None


def _append_unique(
    errors: list[str], seen: set[str], message: str
) -> None:
    if message not in seen:
        errors.append(message)
        seen.add(message)


def instructions() -> None:
    fail(instruction_errors())
    print("Public client configuration commands are usable and secret-explicit.")


def strip_archive_root(names: list[str]) -> list[PurePosixPath]:
    paths = [PurePosixPath(name) for name in names if name and not name.endswith("/")]
    roots = {path.parts[0] for path in paths}
    if len(roots) != 1:
        raise SystemExit("archive does not have one root directory")
    return [PurePosixPath(*path.parts[1:]) for path in paths]


def archive_versions(sdist: Path, wheel: Path) -> tuple[str | None, str | None]:
    with tarfile.open(sdist) as archive:
        pkg_info = next(
            (member for member in archive.getmembers() if member.name.endswith("/PKG-INFO")),
            None,
        )
        extracted = archive.extractfile(pkg_info) if pkg_info is not None else None
        sdist_version = (
            email.parser.BytesParser().parsebytes(extracted.read()).get("Version")
            if extracted is not None
            else None
        )
    with zipfile.ZipFile(wheel) as archive:
        metadata_name = next(
            (name for name in archive.namelist() if name.endswith(".dist-info/METADATA")),
            None,
        )
        wheel_version = (
            email.parser.BytesParser().parsebytes(archive.read(metadata_name)).get("Version")
            if metadata_name is not None
            else None
        )
    return sdist_version, wheel_version


def skill_tree_mismatches(target: Path, *, source: Path) -> list[str]:
    expected = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    actual = {
        path.relative_to(target).as_posix(): path.read_bytes()
        for path in target.rglob("*")
        if path.is_file()
    } if target.is_dir() else {}
    messages: list[str] = []
    for name in sorted(expected):
        if name not in actual:
            messages.append(f"missing skill file: {name}")
        elif actual[name] != expected[name]:
            messages.append(f"changed skill file: {name}")
    messages.extend(
        f"unexpected skill file: {name}" for name in sorted(set(actual) - set(expected))
    )
    return messages


def archive_skill_mismatches(wheel: Path, *, source: Path) -> list[str]:
    expected = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    with zipfile.ZipFile(wheel) as archive:
        actual = {
            str(PurePosixPath(name).relative_to(SKILL_ARCHIVE_ROOT)): archive.read(name)
            for name in archive.namelist()
            if not name.endswith("/")
            and PurePosixPath(name).is_relative_to(SKILL_ARCHIVE_ROOT)
        }
    messages: list[str] = []
    for name in sorted(expected):
        if name not in actual:
            messages.append(f"missing skill file: {name}")
        elif actual[name] != expected[name]:
            messages.append(f"changed skill file: {name}")
    messages.extend(
        f"unexpected skill file: {name}" for name in sorted(set(actual) - set(expected))
    )
    return messages


def archives(dist_dir: Path | None = None) -> None:
    release_dir = dist_dir or ROOT / "dist"
    sdists = sorted(release_dir.glob("*.tar.gz"))
    wheels = sorted(release_dir.glob("*.whl"))
    if len(sdists) != 1 or len(wheels) != 1:
        raise SystemExit("dist must contain exactly one sdist and one wheel")

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    expected_version = project["version"]
    sdist_version, wheel_version = archive_versions(sdists[0], wheels[0])

    with tarfile.open(sdists[0]) as archive:
        sdist_paths = strip_archive_root(archive.getnames())
    bad_sdist = [
        str(path)
        for path in sdist_paths
        if str(path) not in SDIST_FILES and not str(path).startswith("src/things_orchestrator/")
    ]

    with zipfile.ZipFile(wheels[0]) as archive:
        wheel_paths = [PurePosixPath(name) for name in archive.namelist() if not name.endswith("/")]
    bad_wheel = [
        str(path)
        for path in wheel_paths
        if path.parts[0] != "things_orchestrator"
        and not path.parts[0].endswith(".dist-info")
    ]

    errors = [f"sdist contains an unapproved file: {path}" for path in bad_sdist]
    errors += [f"wheel contains an unapproved file: {path}" for path in bad_wheel]
    if sdist_version != expected_version:
        errors.append(
            f"sdist version {sdist_version!r} differs from pyproject.toml {expected_version!r}"
        )
    if wheel_version != expected_version:
        errors.append(
            f"wheel version {wheel_version!r} differs from pyproject.toml {expected_version!r}"
        )
    errors.extend(
        archive_skill_mismatches(
            wheels[0], source=ROOT / "plugin/skills/things-orchestrator"
        )
    )
    fail(errors)
    print("Release archives contain only approved files.")


def bundle() -> None:
    from things_orchestrator.client_bundle import (
        RECEIVER_INSTRUCTION_PATH,
        encode_client_bundle,
        is_named_routine_template,
        parse_client_bundle,
    )
    from things_orchestrator.routines_config import ROUTINE_RECEIVER_INSTRUCTION
    from things_orchestrator.tools import advertised_tool_payload, advertised_tools

    first = encode_client_bundle()
    second = encode_client_bundle()
    errors: list[str] = []
    if first != second:
        errors.append("client bundle is not deterministic")
    try:
        parsed = parse_client_bundle(first)
    except ValueError as error:
        errors.append(f"client bundle failed validation: {error}")
        fail(errors)
        return
    skill = ROOT / "src/things_orchestrator/skills/things-orchestrator"
    expected = {
        path.relative_to(skill).as_posix()
        for path in skill.rglob("*")
        if path.is_file()
    }
    expected.add(RECEIVER_INSTRUCTION_PATH)
    actual = {item.path for item in parsed.files}
    errors.extend(f"client bundle is missing file: {name}" for name in sorted(expected - actual))
    errors.extend(
        f"client bundle has unexpected file: {name}"
        for name in sorted(actual - expected)
    )
    contents = {item.path: item.content for item in parsed.files}
    for name in sorted(expected - {RECEIVER_INSTRUCTION_PATH}):
        if contents.get(name) != (skill / name).read_text(encoding="utf-8"):
            errors.append(f"client bundle file content differs: {name}")
    if contents.get(RECEIVER_INSTRUCTION_PATH) != ROUTINE_RECEIVER_INSTRUCTION:
        errors.append("client bundle receiver instruction differs from routines_config.py")
    expected_tools = [advertised_tool_payload(tool) for tool in advertised_tools()]
    if list(parsed.advertised_tools) != expected_tools:
        errors.append("client bundle advertised tools differ from the canonical tools")
    templates = {
        item.path for item in parsed.files if is_named_routine_template(item.path)
    }
    if set(parsed.component_hashes.routine_templates) != templates:
        errors.append("client bundle routine template hashes do not match named templates")
    fail(errors)
    print("Client bundle is deterministic and complete.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "check", choices=("metadata", "links", "instructions", "archives", "bundle")
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        help="directory containing one sdist and wheel for the archives check",
    )
    args = parser.parse_args()
    selected = args.check
    if selected == "archives":
        archives(args.dist_dir)
        return
    if selected == "bundle":
        bundle()
        return
    {"metadata": metadata, "links": links, "instructions": instructions}[selected]()


if __name__ == "__main__":
    main()
