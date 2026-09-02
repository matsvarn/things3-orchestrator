"""Owner commands: login, configure, serve, print-config, and doctor."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from functools import partial
from getpass import getpass
from pathlib import Path
from secrets import token_urlsafe
from typing import Any, TextIO, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio

from .client_config import ClientKind, Endpoint, render_client_config
from .cloud import (
    CloudClient,
    CloudError,
    CloudLibrary,
)
from .config import (
    ConfigError,
    McpBearer,
    credentials_path,
    launcher_path,
    load_credentials,
    load_mcp_url,
    load_preferences,
    load_source_schemes,
    load_timezone,
    normalize_mcp_url,
    save_credentials,
    save_launcher,
    save_preferences,
)
from .context import SQLiteContextStore
from .deployment import skill_path
from .doctor import DoctorFailure, curl_tool_count_command, run_doctor
from .journal import SQLiteJournal, journal_path
from .server import ThingsMCPServer
from .service import resolve_console_script, service_action
from .workspace import ThingsWorkspace

_LOGIN = (
    "Run `things-orchestrator login` in a private terminal. "
    "Clone development may use `uv run things-orchestrator login`."
)
_LOOPBACK_URL = "http://127.0.0.1:8787/mcp"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Things Cloud MCP server with eight bounded v2 tools.",
        epilog=(
            "Install an exact Git tag, then run things-orchestrator login, "
            "service install, doctor --wait, and print-config --client CLIENT. "
            "Clone development uses the same commands through uv run."
        ),
    )
    commands = parser.add_subparsers(
        dest="action",
        required=True,
        metavar="{login,configure,service,serve,serve-http,print-config,doctor,skill-path,owner-factor,migration-report,legacy-reconcile,legacy-resolve,operation-show,operation-reconcile}",
    )
    login = commands.add_parser("login", help="store Things Cloud email and password (TTY only)")
    login.add_argument(
        "--url",
        "--public-url",
        dest="public_url",
        default="",
        help="HTTPS origin or /mcp URL saved as the canonical MCP endpoint",
    )
    login.add_argument(
        "--timezone",
        default=None,
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
        help="deprecated; use print-config --client CLIENT --show-secrets",
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
    configure.add_argument("--timezone", help="owner IANA timezone")
    configure.add_argument(
        "--url", dest="public_url", help="HTTPS origin or /mcp URL"
    )
    commands.add_parser("serve", help="MCP on stdio")
    commands.add_parser("skill-path", help="print the installed Things skill directory")
    service = commands.add_parser(
        "service", help="install, uninstall, or inspect the supervised HTTP service"
    )
    service_commands = service.add_subparsers(dest="service_action", required=True)
    for action in ("install", "uninstall", "status"):
        service_command = service_commands.add_parser(action)
        if action != "status":
            service_command.add_argument(
                "--dry-run",
                action="store_true",
                help="show the convergent service effects without applying them",
            )
    http = commands.add_parser("serve-http", help="MCP on loopback HTTP behind TLS")
    http.add_argument("--port", type=int, default=8787)
    show = commands.add_parser("print-config", help="render one client configuration")
    show.add_argument(
        "--client",
        choices=tuple(client.value for client in ClientKind),
        help="client whose configuration to render",
    )
    show.add_argument("--http", action="store_true", help=argparse.SUPPRESS)
    show.add_argument(
        "--url",
        dest="public_url",
        default="",
        help="render with this HTTPS origin or /mcp URL without saving it",
    )
    show.add_argument(
        "--show-secrets",
        action="store_true",
        help="substitute the stored MCP bearer for the safe placeholder",
    )
    doctor = commands.add_parser(
        "doctor",
        help="verify deployment identity and an authenticated MCP round trip",
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
        help="also verify this HTTPS origin or /mcp endpoint",
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
            client=args.client,
            show_secrets=args.show_secrets,
        )
        return
    if args.action == "configure":
        if all(
            value is None
            for value in (
                args.note_style,
                args.source_schemes,
                args.timezone,
                args.public_url,
            )
        ):
            parser.error(
                "configure needs --note-style, --source-schemes, --timezone, or --url"
            )
        try:
            path = save_preferences(
                note_style=args.note_style,
                source_schemes=args.source_schemes,
                timezone=args.timezone,
                mcp_url=args.public_url,
            )
            saved_schemes = (
                load_source_schemes(path=path)
                if args.source_schemes is not None
                else None
            )
        except ConfigError as error:
            parser.error(str(error))
            return
        if args.note_style is not None:
            print(f"Note style: {args.note_style}")
        if saved_schemes is not None:
            schemes = ", ".join(saved_schemes)
            print(f"Source schemes: {schemes or 'none'}")
        if args.timezone is not None:
            print(f"Timezone: {args.timezone}")
        if args.public_url is not None:
            print(f"MCP URL: {normalize_mcp_url(args.public_url)}")
        print(f"Stored preferences in {path} (mode 0600).")
        if args.timezone is not None:
            print(
                "Restart the HTTP service to apply the timezone: "
                "things-orchestrator service install"
            )
        else:
            print("The next request uses these preferences. No server restart is needed.")
        return
    if args.action == "doctor":
        _doctor(parser, wait=args.wait, public_url=args.public_url)
        return
    if args.action == "skill-path":
        print(skill_path())
        return
    if args.action == "service":
        dry_run = getattr(args, "dry_run", False)
        try:
            result = service_action(args.service_action, dry_run=dry_run)
        except (ConfigError, OSError) as error:
            parser.error(str(error))
            return
        for effect in result.effects:
            prefix = "Would" if dry_run else "Applied"
            print(f"{prefix}: {effect.description}")
        status_label = "service (planned)" if dry_run else "service"
        print(f"{status_label}: {result.status.value}")
        if args.service_action == "install":
            if dry_run:
                print("Next: rerun without --dry-run to apply this plan")
            else:
                print("Next: things-orchestrator doctor --wait")
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
    try:
        bearer = load_credentials().bearer
    except ConfigError:
        bearer = None
    if bearer is None:
        parser.error("serve-http needs mcp_token from login")
    server.run_http(port=args.port, token=bearer.reveal())


def _login(
    parser: argparse.ArgumentParser,
    *,
    public_url: str,
    rotate_token: bool,
    timezone_name: str | None,
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
    timezone_name = _login_timezone(parser, timezone_name)
    try:
        CloudClient(email, password).verify()
    except CloudError as error:
        parser.error(str(error))
    creds = credentials_path()
    token = _mcp_token(rotate=rotate_token, path=creds)
    preferences_file = creds.with_name("preferences.json")
    existing_url = load_mcp_url(
        preferences_file=preferences_file,
        credentials_file=creds,
    )
    try:
        mcp_url = (
            normalize_mcp_url(public_url)
            if public_url.strip()
            else existing_url or normalize_mcp_url("http://127.0.0.1:8787")
        )
        save_preferences(
            timezone=timezone_name,
            mcp_url=mcp_url,
            path=preferences_file,
        )
    except ConfigError as error:
        parser.error(str(error))
    path = save_credentials(email, password, McpBearer(token), path=creds)
    launcher = save_launcher(resolve_console_script(), path=launcher_path())
    print(f"Stored credentials in {path} (mode 0600, plaintext password).")
    print(f"Stored preferences in {preferences_file} (mode 0600).")
    print(f"Bound the Codex plugin launcher in {launcher} (mode 0600).")
    if rotate_token:
        print("mcp_token rotated. Update every HTTP client header.")
    if show_secrets:
        print("--show-secrets moved to print-config --client CLIENT --show-secrets.")
    print("The HTTP Bearer is the MCP token, not the Cloud password.")
    print("Next: install the HTTP service, run doctor --wait, then render a client config.")
    print("Do not paste the Cloud password into chat.")


def _print_config(
    parser: argparse.ArgumentParser,
    *,
    public_url: str,
    client: str | None,
    show_secrets: bool,
) -> None:
    creds = credentials_path()
    try:
        credentials = load_credentials(path=creds)
    except ConfigError:
        parser.error(_LOGIN)
        return
    if credentials.bearer is None:
        parser.error(_LOGIN)
        return
    preferences_file = creds.with_name("preferences.json")
    try:
        url = (
            normalize_mcp_url(public_url)
            if public_url.strip()
            else load_mcp_url(
                preferences_file=preferences_file,
                credentials_file=creds,
            )
            or normalize_mcp_url("http://127.0.0.1:8787")
        )
        kind = ClientKind(client or ClientKind.CURSOR.value)
        rendered = render_client_config(
            kind,
            Endpoint(url, credentials.bearer),
            skill_dir=skill_path(),
            show_secrets=show_secrets,
        )
    except ConfigError as error:
        parser.error(str(error))
        return
    if client is None:
        print(
            "No --client selected; rendering generic HTTP JSON (deprecated default).",
            file=sys.stderr,
        )
    print(rendered.guidance, file=sys.stderr)
    print(rendered.body, end="")
    if rendered.secondary_body is not None:
        print("Alternative JSON:", file=sys.stderr)
        print(rendered.secondary_body, end="")
    if not show_secrets:
        print(
            "Token is hidden. Add --show-secrets only in a private terminal.",
            file=sys.stderr,
        )
    print(
        "The HTTP Bearer is the MCP token, not the Cloud password.",
        file=sys.stderr,
    )
    print("Do not paste the Cloud password into chat.", file=sys.stderr)


def _mcp_token(*, rotate: bool, path: Path) -> str:
    if not rotate:
        try:
            existing = load_credentials(path=path).bearer
        except ConfigError:
            existing = None
        if existing is not None:
            return existing.reveal()
    return token_urlsafe(32)


def _doctor(
    parser: argparse.ArgumentParser, *, wait: bool, public_url: str
) -> None:
    creds = credentials_path()
    try:
        credentials = load_credentials(path=creds)
    except ConfigError:
        parser.error(_LOGIN)
        return
    if credentials.bearer is None:
        parser.error(_LOGIN)
        return
    print(f"credentials: ok ({credentials.email})")
    timezone_name = load_timezone(
        preferences_file=creds.with_name("preferences.json"),
        credentials_file=creds,
    )
    if not timezone_name:
        print("timezone: missing - run login --timezone Europe/Berlin")
        raise SystemExit(1)
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        print(f"timezone: invalid ({timezone_name})")
        raise SystemExit(1) from None
    print(f"timezone: ok ({timezone_name})")
    stored_url = load_mcp_url(
        preferences_file=creds.with_name("preferences.json"),
        credentials_file=creds,
    )
    hosted = stored_url is not None and stored_url.origin != "http://127.0.0.1:8787"
    if timezone_name == "UTC" and (public_url.strip() or hosted):
        print("timezone: warning - UTC is unusual for a hosted owner account")

    try:
        targets = [normalize_mcp_url(_LOOPBACK_URL)]
        if public_url.strip():
            remote = normalize_mcp_url(public_url)
            if remote not in targets:
                targets.append(remote)
        report = anyio.run(
            partial(
                run_doctor,
                targets,
                credentials.bearer,
                wait=wait,
            )
        )
    except (ConfigError, DoctorFailure) as error:
        print(f"doctor: fail ({error})")
        raise SystemExit(1) from None

    for receipt in report.targets:
        commit = str(receipt.detailed_health["commit"])
        print(
            f"mcp: ok ({receipt.url}; {len(receipt.tool_names)} tools; "
            f"commit {commit[:12]})"
        )
    print("service: current")
    client_target = report.targets[-1].url
    print("Client acceptance (set THINGS_MCP_TOKEN on that machine):")
    print(curl_tool_count_command(client_target))


def _server(parser: argparse.ArgumentParser) -> ThingsMCPServer:
    return ThingsMCPServer(_workspace(parser))


def _workspace(parser: argparse.ArgumentParser) -> ThingsWorkspace:
    try:
        credentials = load_credentials()
    except ConfigError:
        parser.error(_LOGIN)
    email = credentials.email
    library = CloudLibrary(CloudClient(email, credentials.password))
    timezone_name = load_timezone()
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
        email = load_credentials().email
    except ConfigError:
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


def _login_timezone(parser: argparse.ArgumentParser, explicit: str | None) -> str:
    candidate = explicit or _local_timezone_name()
    if explicit is None and candidate == "UTC":
        candidate = input(
            "Owner timezone (IANA, for example Europe/Berlin): "
        ).strip()
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        parser.error("--timezone needs an IANA name such as Europe/Berlin")
    return candidate


if __name__ == "__main__":
    main()
