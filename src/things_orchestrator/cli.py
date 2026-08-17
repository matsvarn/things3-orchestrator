"""Owner commands: login, serve, serve-http, print-config."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from getpass import getpass
from pathlib import Path
from secrets import token_urlsafe
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .cloud import (
    CloudClient,
    CloudError,
    CloudLibrary,
    _ensure_private_dir,
    credentials_path,
    load_credentials,
    load_timezone,
    save_credentials,
    state_cache_path,
)
from .context import SQLiteContextStore
from .journal import SQLiteJournal, journal_path
from .server import ThingsMCPServer
from .workspace import ThingsWorkspace

_LOGIN = "From the clone, run `uv run things-orchestrator login` in a private terminal."
_PLACEHOLDER_HOST = "https://YOUR-HOST/mcp"


class Snippets(NamedTuple):
    stdio: Path
    http: Path
    hermes: Path
    hermes_http: Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Things Cloud MCP server. Three tools: read, commit, and approve.",
        epilog="From the clone: uv run things-orchestrator login. This Mac: merge the Hermes YAML into ~/.hermes/config.yaml. VPS: docs/host.md.",
    )
    commands = parser.add_subparsers(
        dest="action", required=True, metavar="{login,serve,serve-http,print-config}"
    )
    login = commands.add_parser("login", help="store Things Cloud email and password (TTY only)")
    login.add_argument(
        "--url",
        "--public-url",
        dest="public_url",
        default="",
        help="HTTPS origin or /mcp URL written into the HTTP snippets",
    )
    login.add_argument(
        "--timezone",
        default=os.environ.get("THINGS_TIMEZONE") or _local_timezone_name(),
        help="owner IANA timezone, for example Europe/Berlin",
    )
    login.add_argument(
        "--rotate-token",
        action="store_true",
        help="mint a new mcp_token (existing HTTP clients will 401 until they paste it)",
    )
    commands.add_parser("serve", help="MCP on stdio")
    http = commands.add_parser("serve-http", help="MCP on loopback HTTP behind TLS")
    http.add_argument("--port", type=int, default=8787)
    show = commands.add_parser("print-config", help="reprint MCP snippets without logging in")
    show.add_argument("--http", action="store_true", help="print only the HTTP snippets")
    show.add_argument(
        "--url",
        dest="public_url",
        default="",
        help="HTTPS origin or /mcp URL written into the HTTP snippets",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.action == "login":
        _login(
            parser,
            public_url=args.public_url,
            rotate_token=args.rotate_token,
            timezone_name=args.timezone,
        )
        return
    if args.action == "print-config":
        _print_config(parser, public_url=args.public_url, http_only=args.http)
        return
    server = _server(parser)
    if args.action == "serve":
        server.run()
        return
    token = os.environ.get("THINGS_MCP_TOKEN")
    if not token:
        try:
            _, _, token = load_credentials()
        except CloudError:
            token = None
    if not token:
        parser.error("serve-http needs THINGS_MCP_TOKEN or mcp_token from login")
    server.run_http(port=args.port, token=token)


def _login(
    parser: argparse.ArgumentParser,
    *,
    public_url: str,
    rotate_token: bool,
    timezone_name: str,
) -> None:
    if not sys.stdin.isatty():
        parser.error("login needs an interactive terminal. Do not paste the password into chat.")
    email = input("Things Cloud email: ").strip()
    password = getpass("Things Cloud password: ")
    confirm = getpass("Confirm password: ")
    if not email or not password:
        parser.error("email and password are required")
    if password != confirm:
        parser.error("password confirmation did not match")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        parser.error("--timezone needs an IANA name such as Europe/Berlin")
    try:
        CloudClient(email, password).verify()
    except CloudError as error:
        parser.error(str(error))
    creds = credentials_path()
    token = _mcp_token(rotate=rotate_token, path=creds)
    path = save_credentials(
        email,
        password,
        token,
        timezone_name=timezone_name,
        path=creds,
    )
    _remember_checkout()
    snippets = _write_mcp_snippets(path.parent, token=token, public_url=public_url)
    print(f"Stored credentials in {path} (mode 0600, plaintext password).")
    _print_snippets(snippets, http_only=False)
    if rotate_token:
        print("mcp_token rotated. Update every HTTP client header.")
    print("The HTTP Bearer is the MCP token, not the Cloud password.")
    print("Next: docs/host.md if the server is a VPS, else docs/clients.md.")
    print("Do not paste the Cloud password into chat.")


def _print_config(
    parser: argparse.ArgumentParser, *, public_url: str, http_only: bool
) -> None:
    creds = credentials_path()
    try:
        _email, _password, token = load_credentials(path=creds)
    except CloudError:
        parser.error(_LOGIN)
        return
    if not token:
        parser.error(_LOGIN)
        return
    _remember_checkout()
    snippets = _write_mcp_snippets(creds.parent, token=token, public_url=public_url)
    _print_snippets(snippets, http_only=http_only)
    print("The HTTP Bearer is the MCP token, not the Cloud password.")
    print("Do not paste the Cloud password into chat.")


def _mcp_token(*, rotate: bool, path: Path) -> str:
    if not rotate:
        try:
            _email, _password, existing = load_credentials(path=path)
        except CloudError:
            existing = None
        if existing:
            return existing
    return token_urlsafe(32)


def _http_url(public_url: str) -> str:
    raw = public_url.strip().rstrip("/")
    if not raw:
        return _PLACEHOLDER_HOST
    if raw.endswith("/mcp"):
        return raw
    return f"{raw}/mcp"


def _read_existing_http_url(config_dir: Path) -> str:
    path = config_dir / "mcp.http.json"
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text())
        url = payload["mcpServers"]["things"]["url"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return ""
    if isinstance(url, str) and url and "YOUR-HOST" not in url:
        return url
    return ""


def _resolved_http_url(config_dir: Path, public_url: str) -> str:
    if public_url.strip():
        return _http_url(public_url)
    return _read_existing_http_url(config_dir) or _PLACEHOLDER_HOST


def _yaml_quote(value: str) -> str:
    return json.dumps(value)


def _skills_dir() -> Path:
    return _checkout_wrapper().parent.parent / "skills"


def _stdio_mcp() -> dict[str, object]:
    return {
        "mcpServers": {
            "things": {
                "command": str(_checkout_wrapper()),
                "args": ["serve"],
            }
        }
    }


def _http_mcp(token: str, url: str) -> dict[str, object]:
    return {
        "mcpServers": {
            "things": {
                "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }


def _hermes_stdio_yaml() -> str:
    return (
        "mcp_servers:\n"
        "  things:\n"
        f"    command: {_yaml_quote(str(_checkout_wrapper()))}\n"
        '    args: ["serve"]\n'
        "    tools:\n"
        "      resources: false\n"
        "      prompts: false\n"
        "\n"
        "skills:\n"
        "  external_dirs:\n"
        f"    - {_yaml_quote(str(_skills_dir()))}\n"
    )


def _hermes_http_yaml(token: str, url: str) -> str:
    return (
        "mcp_servers:\n"
        "  things:\n"
        f"    url: {_yaml_quote(url)}\n"
        "    headers:\n"
        f"      Authorization: {_yaml_quote(f'Bearer {token}')}\n"
        "    tools:\n"
        "      resources: false\n"
        "      prompts: false\n"
        "\n"
        "skills:\n"
        "  external_dirs:\n"
        f"    - {_yaml_quote(str(_skills_dir()))}\n"
    )


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    _ensure_private_dir(path.parent)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    path.chmod(0o600)
    return path


def _write_text(path: Path, text: str) -> Path:
    _ensure_private_dir(path.parent)
    path.write_text(text)
    path.chmod(0o600)
    return path


def _write_mcp_snippets(config_dir: Path, *, token: str, public_url: str) -> Snippets:
    url = _resolved_http_url(config_dir, public_url)
    stdio = _write_json(config_dir / "mcp.stdio.json", _stdio_mcp())
    http = _write_json(config_dir / "mcp.http.json", _http_mcp(token, url))
    hermes = _write_text(config_dir / "mcp.hermes.yaml", _hermes_stdio_yaml())
    hermes_http = _write_text(config_dir / "mcp.hermes.http.yaml", _hermes_http_yaml(token, url))
    return Snippets(stdio=stdio, http=http, hermes=hermes, hermes_http=hermes_http)


def _print_snippets(snippets: Snippets, *, http_only: bool) -> None:
    http_body = snippets.http.read_text()
    placeholder = "YOUR-HOST" in http_body
    if not http_only:
        print("Hermes (this Mac): merge into ~/.hermes/config.yaml")
        print(f"Wrote {snippets.hermes}")
        print(snippets.hermes.read_text(), end="")
        print(f"Cursor / Codex / Claude Desktop JSON: {snippets.stdio}")
        print(json.dumps(json.loads(snippets.stdio.read_text()), indent=2))
        if placeholder:
            print("VPS: uv run things-orchestrator print-config --http --url https://YOUR-HOST")
            print("On another machine, change skills.external_dirs to a local copy of plugin/skills.")
            print("Numbered host steps: docs/host.md.")
            return
    print("Hermes (VPS): merge into ~/.hermes/config.yaml")
    print(f"Wrote {snippets.hermes_http}")
    print(snippets.hermes_http.read_text(), end="")
    print("On another machine, change skills.external_dirs to a local copy of plugin/skills.")
    print(f"Cursor Cloud Agents / Claude Code JSON: {snippets.http}")
    print(json.dumps(json.loads(http_body), indent=2))
    if placeholder:
        print("Replace YOUR-HOST, or rerun with --url https://your-host.")


def _checkout_wrapper() -> Path:
    return Path(__file__).resolve().parents[2] / "plugin" / "bin" / "things-orchestrator"


def _remember_checkout() -> None:
    root = Path(__file__).resolve().parents[2]
    if not (root / "pyproject.toml").is_file():
        return
    path = state_cache_path().with_name("checkout")
    _ensure_private_dir(path.parent)
    path.write_text(f"{root}\n")
    path.chmod(0o600)


def _server(parser: argparse.ArgumentParser) -> ThingsMCPServer:
    try:
        email, password, _token = load_credentials()
    except CloudError:
        parser.error(_LOGIN)
    library = CloudLibrary(CloudClient(email, password))
    timezone_name = load_timezone() or os.environ.get("THINGS_TIMEZONE")
    try:
        timezone = ZoneInfo(timezone_name) if timezone_name else datetime.now().astimezone().tzinfo
    except ZoneInfoNotFoundError:
        parser.error("Stored timezone is invalid. Run login --timezone Europe/Berlin.")

    def clock() -> datetime:
        return datetime.now(timezone)

    account_journal = journal_path(email)
    context_path = account_journal.with_name(
        account_journal.name.replace("journal", "contexts", 1)
    )
    return ThingsMCPServer(
        ThingsWorkspace(
            library,
            journal=SQLiteJournal(account_journal),
            clock=clock,
            context_store=SQLiteContextStore(
                context_path,
                clock=clock,
                token_factory=lambda: token_urlsafe(24),
            ),
            account_id=email,
        )
    )


def _local_timezone_name() -> str:
    timezone = datetime.now().astimezone().tzinfo
    key = getattr(timezone, "key", None)
    return str(key) if key else "UTC"


if __name__ == "__main__":
    main()
