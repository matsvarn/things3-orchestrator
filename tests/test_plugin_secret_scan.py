from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_plugin_contains_no_secret_transport_or_host_inspection_recipe() -> None:
    sources = "\n".join(
        path.read_text(errors="replace")
        for path in (ROOT / "plugin").rglob("*")
        if path.is_file()
    )

    assert "auth-token" not in sources
    assert "security find-generic-password" not in sources
    assert "~/Library" not in sources
    assert "THINGS_PASSWORD" not in sources
    assert "BEGIN PRIVATE KEY" not in sources
