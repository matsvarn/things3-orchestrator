"""Keep client-only commands independent of Unix host modules."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == "client-sync":
        from .client_sync import main as client_main

        client_main(arguments[1:])
        return
    if sys.platform == "win32":
        if not arguments or arguments == ["--help"] or arguments == ["-h"]:
            from .client_sync import main as client_main

            client_main(["--help"])
            return
        raise SystemExit("Host commands require macOS or Linux. Use client-sync to connect to a host.")
    from .cli import main as host_main

    host_main(arguments)
