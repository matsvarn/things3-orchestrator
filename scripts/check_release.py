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
from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
GIT_INSTALL_TAG = re.compile(
    r"\Agit\+https://[^\s]+/things3-orchestrator\.git@(?P<tag>v[^\s]+)\Z"
)
SDIST_FILES = {".gitignore", "LICENSE", "PKG-INFO", "README.md", "pyproject.toml"}
SKILL_ARCHIVE_ROOT = PurePosixPath("things_orchestrator/skills/things-orchestrator")


def fail(messages: list[str]) -> None:
    if messages:
        raise SystemExit("\n".join(messages))


def metadata() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    manifest = json.loads((ROOT / "plugin/.codex-plugin/plugin.json").read_text())
    errors: list[str] = []
    errors.extend(marketplace_errors())
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
    for path, required_kind in install_guides.items():
        errors.extend(
            install_tag_errors(
                path.read_text(),
                source=path.relative_to(ROOT),
                version=project["version"],
                required_kind=required_kind,
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
        for number, tokens in shell_commands(source.read_text()):
            command = _client_config_command(tokens)
            if command is not None and "--show-secrets" not in command:
                errors.append(
                    f"{source.relative_to(root)}:{number}: usable client config needs "
                    "--show-secrets"
                )
    return errors


def install_tag_errors(
    markdown: str,
    *,
    source: Path,
    version: str,
    required_kind: str,
) -> list[str]:
    expected = f"v{version}"
    found_required = False
    errors: list[str] = []
    for number, tokens in shell_commands(markdown):
        uv_index = _subsequence_index(tokens, ("uv", "tool", "install"))
        if uv_index is not None and any(
            "things3-orchestrator" in token for token in tokens[uv_index + 3 :]
        ):
            found_required = found_required or required_kind == "uv"
            tags = [
                match.group("tag")
                for token in tokens[uv_index + 3 :]
                if (match := GIT_INSTALL_TAG.fullmatch(token)) is not None
            ]
            if not tags:
                errors.append(
                    f"{source}:{number}: uv install needs exact tag {expected}"
                )
            else:
                errors.extend(
                    f"{source}:{number}: uv install tag {tag} differs from {expected}"
                    for tag in tags
                    if tag != expected
                )

        codex_index = _subsequence_index(
            tokens, ("codex", "plugin", "marketplace", "add")
        )
        if codex_index is not None and any(
            token == "matsvarn/things3-orchestrator"
            for token in tokens[codex_index + 4 :]
        ):
            found_required = found_required or required_kind == "codex"
            references = _option_values(tokens[codex_index + 4 :], "--ref")
            if not references or any(reference is None for reference in references):
                errors.append(
                    f"{source}:{number}: Codex marketplace install needs exact ref "
                    f"{expected}"
                )
            errors.extend(
                f"{source}:{number}: Codex marketplace ref {reference} differs "
                f"from {expected}"
                for reference in references
                if reference is not None and reference != expected
            )
    if not found_required:
        errors.append(f"{source}: missing {required_kind} install for {expected}")
    return errors


def shell_commands(markdown: str) -> list[tuple[int, tuple[str, ...]]]:
    commands: list[tuple[int, tuple[str, ...]]] = []
    fragments: list[str] = []
    start = 1
    for number, raw_line in enumerate(markdown.splitlines(), start=1):
        line = raw_line.strip()
        if not fragments:
            start = number
        continued = line.endswith("\\")
        fragments.append(line[:-1].rstrip() if continued else line)
        if continued:
            continue
        logical = " ".join(fragment for fragment in fragments if fragment)
        fragments = []
        if not logical:
            continue
        commands.extend((start, segment) for segment in _shell_segments(logical))
    if fragments:
        logical = " ".join(fragment for fragment in fragments if fragment)
        commands.extend((start, segment) for segment in _shell_segments(logical))
    return commands


def _shell_segments(command: str) -> list[tuple[str, ...]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.commenters = "#"
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    result: list[tuple[str, ...]] = []
    current: list[str] = []
    for token in tokens:
        if token in {";", "&&", "||", "|", "&"}:
            if current:
                result.append(tuple(current))
                current = []
        else:
            current.append(token)
    if current:
        result.append(tuple(current))
    return result


def _client_config_command(tokens: tuple[str, ...]) -> tuple[str, ...] | None:
    start = _subsequence_index(tokens, ("things-orchestrator", "print-config"))
    if start is None:
        return None
    command = tokens[start:]
    client = _option_value(command[2:], "--client")
    if client not in {"codex", "hermes", "claude-code", "cursor", "cursor-cloud"}:
        return None
    return command


def _subsequence_index(
    tokens: tuple[str, ...], expected: tuple[str, ...]
) -> int | None:
    width = len(expected)
    return next(
        (
            index
            for index in range(len(tokens) - width + 1)
            if tokens[index : index + width] == expected
        ),
        None,
    )


def _option_value(tokens: tuple[str, ...], option: str) -> str | None:
    for index, token in enumerate(tokens):
        if token.startswith(f"{option}="):
            return token.partition("=")[2]
        if token == option and index + 1 < len(tokens):
            return tokens[index + 1]
    return None


def _option_values(
    tokens: tuple[str, ...], option: str
) -> tuple[str | None, ...]:
    values: list[str | None] = []
    for index, token in enumerate(tokens):
        if token.startswith(f"{option}="):
            values.append(token.partition("=")[2] or None)
        elif token == option:
            value = tokens[index + 1] if index + 1 < len(tokens) else None
            values.append(value if value and not value.startswith("-") else None)
    return tuple(values)


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "check", choices=("metadata", "links", "instructions", "archives")
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
    {"metadata": metadata, "links": links, "instructions": instructions}[selected]()


if __name__ == "__main__":
    main()
