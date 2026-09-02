"""Owner commands: login, configure, serve, print-config, and doctor."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from getpass import getpass
from pathlib import Path
from secrets import token_urlsafe
from typing import Any, NamedTuple, TextIO, cast
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
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
from .deployment import skill_path
from .journal import SQLiteJournal, journal_path
from .preferences import (
    PreferencesError,
    load_preferences,
    load_source_schemes,
    save_preferences,
)
from .server import ThingsMCPServer
from .workspace import ThingsWorkspace

_LOGIN = "From the clone, run `uv run things-orchestrator login` in a private terminal."
_PLACEHOLDER_HOST = "https://YOUR-HOST/mcp"
_LOOPBACK_HEALTH = "http://127.0.0.1:8787/health"
_HEALTH_WAIT_SECONDS = 15
_SNIPPET_NAMES = (
    "mcp.stdio.json",
    "mcp.http.json",
    "mcp.hermes.yaml",
    "mcp.hermes.http.yaml",
)


class Snippets(NamedTuple):
    stdio: Path
    http: Path
    hermes: Path
    hermes_http: Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Things Cloud MCP server with eight bounded v2 tools.",
        epilog=(
            "From the clone: uv run things-orchestrator login. "
            "HTTP host: uv run things-orchestrator doctor. "
            "This Mac: merge the Hermes YAML into ~/.hermes/config.yaml. "
            "VPS: docs/host.md."
        ),
    )
    commands = parser.add_subparsers(
        dest="action",
        required=True,
        metavar="{login,configure,serve,serve-http,print-config,doctor,skill-path,owner-factor,migration-report,legacy-reconcile,legacy-resolve,operation-show,operation-reconcile}",
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
    login.add_argument(
        "--show-secrets",
        action="store_true",
        help="print snippet file bodies (includes the MCP bearer)",
    )
    configure = commands.add_parser(
        "configure", help="change owner preferences without changing credentials"
    )
    configure.add_argument(
        "--note-style",
        choices=("natural", "visual"),
        help="default note style for new Projects",
    )
    configure.add_argument(
        "--source-schemes",
        nargs="*",
        help="approved third-party app schemes; pass no values to clear",
    )
    commands.add_parser("serve", help="MCP on stdio")
    commands.add_parser("skill-path", help="print the installed Things skill directory")
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
    show.add_argument(
        "--show-secrets",
        action="store_true",
        help="print snippet file bodies (includes the MCP bearer)",
    )
    doctor = commands.add_parser(
        "doctor",
        help="check credentials, snippets, and serve-http /health",
    )
    doctor.add_argument(
        "--wait",
        action="store_true",
        help="retry loopback /health for about 15 seconds (serve-http readiness)",
    )
    doctor.add_argument(
        "--url",
        dest="public_url",
        default="",
        help="also GET {origin}/health (no bearer)",
    )
    commands.add_parser("owner-factor", help="enroll the signed legacy-recovery passphrase")
    commands.add_parser("migration-report", help="quarantine and report retained v1 operations")
    legacy_reconcile = commands.add_parser("legacy-reconcile", help="classify one retained v1 pending row from Cloud evidence without replay")
    legacy_reconcile.add_argument("intent_id")
    legacy_resolve = commands.add_parser("legacy-resolve", help="release one retained v1 partial or unknown fence with signed owner resolution")
    legacy_resolve.add_argument("intent_id")
    legacy_resolve.add_argument("resolution", choices=("accepted_as_is", "superseded"))
    operation_show = commands.add_parser("operation-show", help="render one exact operation manifest")
    operation_show.add_argument("operation_id")
    operation_reconcile = commands.add_parser("operation-reconcile", help="force read-back for one pending operation without replay")
    operation_reconcile.add_argument("operation_id")
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
            show_secrets=args.show_secrets,
        )
        return
    if args.action == "print-config":
        _print_config(
            parser,
            public_url=args.public_url,
            http_only=args.http,
            show_secrets=args.show_secrets,
        )
        return
    if args.action == "configure":
        if args.note_style is None and args.source_schemes is None:
            parser.error("configure needs --note-style or --source-schemes")
        try:
            path = save_preferences(
                note_style=args.note_style,
                source_schemes=args.source_schemes,
            )
            saved_schemes = (
                load_source_schemes(path=path)
                if args.source_schemes is not None
                else None
            )
        except PreferencesError as error:
            parser.error(str(error))
            return
        if args.note_style is not None:
            print(f"Note style: {args.note_style}")
        if saved_schemes is not None:
            schemes = ", ".join(saved_schemes)
            print(f"Source schemes: {schemes or 'none'}")
        print(f"Stored preferences in {path} (mode 0600).")
        print("The next Project uses these preferences. No server restart is needed.")
        return
    if args.action == "doctor":
        _doctor(parser, wait=args.wait, public_url=args.public_url)
        return
    if args.action == "skill-path":
        print(skill_path())
        return
    if args.action == "owner-factor":
        _owner_factor(parser)
        return
    if args.action == "migration-report":
        _migration_report(parser)
        return
    if args.action == "legacy-reconcile":
        print(json.dumps(_workspace(parser).host_reconcile_v1_pending(args.intent_id), sort_keys=True))
        return
    if args.action == "legacy-resolve":
        _legacy_resolution_command(parser, args.intent_id, args.resolution)
        return
    if args.action.startswith("operation-"):
        _operation_command(
            parser,
            action=args.action,
            operation_id=args.operation_id,
        )
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
    show_secrets: bool,
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
    _print_snippets(snippets, http_only=False, show_secrets=show_secrets)
    if rotate_token:
        print("mcp_token rotated. Update every HTTP client header.")
    print("The HTTP Bearer is the MCP token, not the Cloud password.")
    print("Next: docs/host.md if the server is a VPS, else docs/clients.md.")
    print("Do not paste the Cloud password into chat.")


def _print_config(
    parser: argparse.ArgumentParser,
    *,
    public_url: str,
    http_only: bool,
    show_secrets: bool,
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
    _print_snippets(snippets, http_only=http_only, show_secrets=show_secrets)
    print("The HTTP Bearer is the MCP token, not the Cloud password.")
    print("Next: docs/host.md if the server is a VPS, else docs/clients.md.")
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


def _print_snippets(snippets: Snippets, *, http_only: bool, show_secrets: bool) -> None:
    http_body = snippets.http.read_text()
    placeholder = "YOUR-HOST" in http_body
    if not http_only:
        _emit_snippet(
            "Hermes stdio YAML: merge into the active Hermes profile "
            "config.yaml (default ~/.hermes/config.yaml)",
            snippets.hermes,
            snippets.hermes.read_text(),
            show_secrets=show_secrets,
        )
        _emit_snippet(
            f"Cursor / Claude Desktop JSON: {snippets.stdio}",
            snippets.stdio,
            json.dumps(json.loads(snippets.stdio.read_text()), indent=2) + "\n",
            show_secrets=show_secrets,
        )
        if placeholder and show_secrets:
            print(
                "VPS: uv run things-orchestrator print-config "
                "--http --show-secrets --url https://YOUR-HOST"
            )
            print(
                "Keep skills.external_dirs on this host when the agent "
                "runtime is here; change it only if the agent runs elsewhere."
            )
            print("Numbered host steps: docs/host.md.")
            _print_secret_hint(http_only=http_only, show_secrets=show_secrets)
            return
    _emit_snippet(
        "Hermes HTTP YAML: merge into the active Hermes profile "
        "config.yaml (default ~/.hermes/config.yaml)",
        snippets.hermes_http,
        snippets.hermes_http.read_text(),
        show_secrets=show_secrets,
    )
    if show_secrets:
        print(
            "Keep skills.external_dirs on this host when the agent "
            "runtime is here; change it only if the agent runs elsewhere."
        )
    _emit_snippet(
        f"Cursor Cloud Agents / Claude Code JSON: {snippets.http}",
        snippets.http,
        json.dumps(json.loads(http_body), indent=2) + "\n",
        show_secrets=show_secrets,
    )
    if placeholder:
        print("HTTP URL still has YOUR-HOST. Rerun with --url https://your-host.")
    _print_secret_hint(http_only=http_only, show_secrets=show_secrets)


def _emit_snippet(label: str, path: Path, body: str, *, show_secrets: bool) -> None:
    if not show_secrets:
        print(f"Wrote {path}")
        return
    print(label)
    print(f"Wrote {path}")
    print(body, end="")


def _print_secret_hint(*, http_only: bool, show_secrets: bool) -> None:
    if show_secrets:
        return
    command = "uv run things-orchestrator print-config --show-secrets"
    if http_only:
        command += " --http"
    print(f"To print snippet contents: {command}")


def _doctor(
    parser: argparse.ArgumentParser, *, wait: bool, public_url: str
) -> None:
    creds = credentials_path()
    try:
        email, _password, token = load_credentials(path=creds)
    except CloudError:
        parser.error(_LOGIN)
        return
    if not token:
        parser.error(_LOGIN)
        return
    print(f"credentials: ok ({email})")
    failed = False

    timezone_name = load_timezone(path=creds)
    if not timezone_name:
        print("timezone: missing")
        failed = True
    else:
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            print(f"timezone: invalid ({timezone_name})")
            failed = True
        else:
            print(f"timezone: ok ({timezone_name})")

    config_dir = creds.parent
    missing = [name for name in _SNIPPET_NAMES if not (config_dir / name).is_file()]
    if missing:
        print(f"snippets: missing {', '.join(missing)}")
        failed = True
    else:
        print("snippets: ok")

    resolved = _resolved_http_url(config_dir, "")
    hosted = "YOUR-HOST" not in resolved
    if hosted:
        print(f"http url: {resolved}")
    else:
        print("http url: placeholder")

    loopback = _probe_loopback(wait=wait)
    print(f"loopback health: {loopback}")
    if loopback != "ok" and (wait or hosted):
        failed = True

    if public_url.strip():
        remote = _probe_health(_origin_health_url(public_url))
        print(f"remote health: {remote}")
        if remote != "ok":
            failed = True

    if failed:
        sys.exit(1)


def _origin_health_url(public_url: str) -> str:
    raw = public_url.strip().rstrip("/")
    if raw.endswith("/mcp"):
        raw = raw[: -len("/mcp")].rstrip("/")
    return f"{raw}/health"


def _probe_loopback(*, wait: bool) -> str:
    deadline = time.monotonic() + _HEALTH_WAIT_SECONDS
    status = _probe_health(_LOOPBACK_HEALTH)
    while wait and status != "ok" and time.monotonic() < deadline:
        time.sleep(1)
        status = _probe_health(_LOOPBACK_HEALTH)
    return status


def _probe_health(url: str, *, timeout: float = 2.0) -> str:
    try:
        with urlopen(url, timeout=timeout) as response:
            raw = response.read()
    except HTTPError:
        return "fail"
    except (URLError, TimeoutError, OSError):
        return "not listening"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return "fail"
    if isinstance(payload, dict) and payload.get("ok") is True:
        return "ok"
    return "fail"


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
    return ThingsMCPServer(_workspace(parser))


def _workspace(parser: argparse.ArgumentParser) -> ThingsWorkspace:
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
    journal = SQLiteJournal(account_journal)
    journal.cutover_v1()
    journal.prune_v2(now=clock().isoformat(), retention_days=7)
    return ThingsWorkspace(
        library,
        journal=journal,
        clock=clock,
        context_store=SQLiteContextStore(
            context_path,
            clock=clock,
            token_factory=lambda: token_urlsafe(24),
        ),
        account_id=email,
        preferences=load_preferences,
    )


def _migration_report(parser: argparse.ArgumentParser) -> None:
    try:
        email, _password, _token = load_credentials()
    except CloudError:
        parser.error(_LOGIN)
        return
    journal = SQLiteJournal(journal_path(email))
    print(json.dumps(journal.cutover_v1(), sort_keys=True))


@contextmanager
def _private_tty(parser: argparse.ArgumentParser) -> Iterator[TextIO]:
    try:
        terminal = open("/dev/tty", "r+", encoding="utf-8")
    except OSError:
        if sys.stdin.isatty() and sys.stderr.isatty():
            yield sys.stderr
            return
        parser.error("This host command needs a private local or SSH terminal.")
    try:
        yield terminal
    finally:
        terminal.close()


def _owner_factor(parser: argparse.ArgumentParser) -> None:
    from .owner_authority import enroll_owner_factor

    with _private_tty(parser) as terminal:
        passphrase = getpass("New owner approval passphrase: ", stream=terminal)
        confirm = getpass("Confirm owner approval passphrase: ", stream=terminal)
    if passphrase != confirm:
        parser.error("owner passphrase confirmation did not match")
    try:
        path = enroll_owner_factor(passphrase)
    except ValueError as error:
        parser.error(str(error))
    print(f"Stored the owner factor verifier in {path} (mode 0600).")


def _operation_command(
    parser: argparse.ArgumentParser,
    *,
    action: str,
    operation_id: str,
) -> None:
    from .owner_authority import render_operation

    workspace = _workspace(parser)
    operation = workspace.host_get_operation_v2(operation_id)
    if operation is None:
        parser.error("operation not found for this account")
    try:
        print(render_operation(operation))
    except ValueError as error:
        parser.error(str(error))
    if action == "operation-show":
        return
    if action == "operation-reconcile":
        print(json.dumps(workspace.host_reconcile_v2(operation_id), sort_keys=True))
        return
    parser.error("unsupported operation command")


def _legacy_resolution_command(
    parser: argparse.ArgumentParser,
    intent_id: str,
    resolution: str,
) -> None:
    from .owner_authority import render_operation, verified_authorization

    workspace = _workspace(parser)
    operation = workspace.host_get_legacy_resolution_v1(intent_id)
    if operation is None:
        parser.error("retained v1 operation is not pending")
    print(render_operation(operation))
    with _private_tty(parser) as terminal:
        passphrase = getpass("Owner approval passphrase: ", stream=terminal)
    try:
        authorization = verified_authorization(
            operation,
            action=f"legacy_{resolution}",
            passphrase=passphrase,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(f"owner factor is unavailable: {error}")
    if authorization is None:
        parser.error("owner factor did not match")
    if not workspace.host_resolve_legacy_v1(intent_id, cast(Any, resolution), authorization):
        parser.error("retained v1 operation cannot be resolved")
    print(f"legacy_resolved: {resolution}")


def _local_timezone_name() -> str:
    timezone = datetime.now().astimezone().tzinfo
    key = getattr(timezone, "key", None)
    return str(key) if key else "UTC"


if __name__ == "__main__":
    main()
