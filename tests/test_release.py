from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

from scripts.check_release import archive_versions


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
