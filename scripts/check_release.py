#!/usr/bin/env python3
"""Check public metadata, local links, and built archive contents."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tarfile
import tomllib
import urllib.parse
import zipfile
from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
SDIST_FILES = {".gitignore", "LICENSE", "PKG-INFO", "README.md", "pyproject.toml"}


def fail(messages: list[str]) -> None:
    if messages:
        raise SystemExit("\n".join(messages))


def metadata() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    manifest = json.loads((ROOT / "plugin/.codex-plugin/plugin.json").read_text())
    errors: list[str] = []
    if project["version"] != manifest.get("version"):
        errors.append("pyproject.toml and plugin.json versions differ")

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

    fail(errors)
    print("Release metadata and skill files are valid.")


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


def strip_archive_root(names: list[str]) -> list[PurePosixPath]:
    paths = [PurePosixPath(name) for name in names if name and not name.endswith("/")]
    roots = {path.parts[0] for path in paths}
    if len(roots) != 1:
        raise SystemExit("archive does not have one root directory")
    return [PurePosixPath(*path.parts[1:]) for path in paths]


def archives() -> None:
    sdists = sorted((ROOT / "dist").glob("*.tar.gz"))
    wheels = sorted((ROOT / "dist").glob("*.whl"))
    if len(sdists) != 1 or len(wheels) != 1:
        raise SystemExit("dist must contain exactly one sdist and one wheel")

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
    fail(errors)
    print("Release archives contain only approved files.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("check", choices=("metadata", "links", "archives"))
    selected = parser.parse_args().check
    {"metadata": metadata, "links": links, "archives": archives}[selected]()


if __name__ == "__main__":
    main()
