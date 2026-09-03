from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

from scripts.check_release import (
    archive_skill_mismatches,
    archive_versions,
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
