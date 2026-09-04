"""Owner command-line interface."""

from __future__ import annotations

import argparse
import json
import shlex
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
    Credentials,
    McpBearer,
    credentials_path,
    launcher_path,
    load_credentials,
    load_legacy_mcp_url,
    load_mcp_url,
    load_preferences,
    load_source_schemes,
    load_timezone,
    normalize_mcp_url,
    save_credentials,
    save_launcher,
    save_preferences,
    select_login_mcp_url,
)
from .deployment import skill_path
from .diagnostics import (
    collect_cloud_check,
    collect_routines_diagnostic,
    collect_service_state,
    collect_support_report,
    probe_routine_runtime,
)
from .doctor import DoctorFailure, curl_tool_count_command, run_doctor
from .journal import SQLiteJournal, journal_path
from .routines import RoutineWorker
from .routines_config import (
    ROUTINE_EVENT_TYPE,
    ROUTINE_RECEIVER_INSTRUCTION,
    ROUTINE_TRIGGER_TAG,
    EnabledRoutineConfig,
    ReceiverSecret,
    account_digest,
    configure_routines,
    load_routines_config,
    routines_config_path,
    routines_status,
    set_routines_enabled,
)
from .routines_store import RoutineStore
from .routines_webhook import build_webhook
from .server import RoutineHTTPComposition, ThingsMCPServer
from .service import ServiceApplyError, resolve_console_script, service_action
from .workspace import ThingsWorkspace

_LOGIN = (
    "Run `things-orchestrator login` in a private terminal. "
    "Clone development may use `uv run things-orchestrator login`."
)
_LOOPBACK_URL = "http://127.0.0.1:8787/mcp"
class _ExactArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["allow_abbrev"] = False
        super().__init__(*args, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = _ExactArgumentParser(
        description="Things Cloud MCP server with eight bounded v2 tools.",
        epilog=(
            "Install an exact Git tag, then run things-orchestrator login, "
            "service install, doctor --wait, and print-config --client CLIENT "
            "--show-secrets in a private terminal. "
            "Clone development uses the same commands through uv run."
        ),
    )
    commands = parser.add_subparsers(
        dest="action",
        required=True,
        metavar="COMMAND",
    )
    login = commands.add_parser(
        "login", help="store Things Cloud email and password (TTY only)"
    )
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
    configure.add_argument("--url", dest="public_url", help="HTTPS origin or /mcp URL")
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
    http = commands.add_parser("serve-http", help="MCP on loopback HTTP")
    http.add_argument("--port", type=int, default=8787)
    http.add_argument("--service-managed", action="store_true", help=argparse.SUPPRESS)
    routines = commands.add_parser(
        "routines", help="set up or operate the optional routines worker"
    )
    routines_commands = routines.add_subparsers(dest="routines_action", required=True)
    routines_setup = routines_commands.add_parser(
        "setup",
        help="configure, enable, and install the supervised routines service",
        description=(
            "Configure, enable, and install the supervised routines service. "
            "Receiver values are entered in a private terminal. "
            "Hermes is the default receiver."
        ),
    )
    routines_setup.add_argument(
        "--profile",
        choices=("always_on",),
        required=True,
        help="host profile for the supervised routines worker",
    )
    routines_setup.add_argument(
        "--receiver",
        choices=("hermes", "grok"),
        default="hermes",
        help="webhook receiver. Hermes is the default",
    )
    routines_setup.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Things Cloud polling interval in seconds (60-3600, default: 60)",
    )
    routines_setup.add_argument(
        "--settle",
        type=int,
        default=120,
        help="event settle window in seconds (1-3600, default: 120)",
    )
    routines_configure = routines_commands.add_parser(
        "configure",
        help="store a disabled routines receiver profile",
        description=(
            "Store a disabled, account-bound routines receiver profile. "
            "Hermes is the default receiver."
        ),
    )
    routines_configure.add_argument(
        "--profile",
        choices=("always_on",),
        required=True,
        help="host profile for the supervised routines worker",
    )
    routines_configure.add_argument(
        "--receiver",
        choices=("hermes", "grok"),
        default="hermes",
        help="webhook receiver. Hermes is the default",
    )
    routines_configure.add_argument(
        "--url",
        help=(
            "receiver webhook URL. Omit it to enter the URL privately "
            "in a private terminal"
        ),
    )
    routines_configure.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Things Cloud polling interval in seconds (60-3600, default: 60)",
    )
    routines_configure.add_argument(
        "--settle",
        type=int,
        default=120,
        help="event settle window in seconds (1-3600, default: 120)",
    )
    routines_commands.add_parser(
        "enable",
        help="enable the saved profile; restart the supervised service to start it",
        description=(
            "Enable the saved account-bound routines profile. This command is "
            "idempotent and does not start a worker until the service restarts."
        ),
    )
    routines_commands.add_parser(
        "disable",
        help="stop new polling and delivery without deleting saved state",
        description=(
            "Disable routines without deleting configuration, candidates, or "
            "delivery history. A running worker observes this change within one poll interval."
        ),
    )
    routines_commands.add_parser(
        "status",
        help="show safe configuration, service, worker, history, and delivery state",
        description=(
            "Read value-free routines status. When an MCP bearer is configured, "
            "the command attempts one bounded, authenticated loopback health probe. "
            "It never creates the routines database."
        ),
    )
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
    commands.add_parser(
        "cloud-check",
        help="read and fold current Cloud state, then print only aggregate counts",
    )
    commands.add_parser(
        "support-bundle",
        help="print value-free deployment diagnostics as stable-schema JSON",
    )
    commands.add_parser(
        "owner-factor", help="enroll the signed legacy-recovery passphrase"
    )
    commands.add_parser(
        "migration-report", help="quarantine and report retained v1 operations"
    )
    legacy_reconcile = commands.add_parser(
        "legacy-reconcile",
        help="classify one retained v1 pending row from Cloud evidence without replay",
    )
    legacy_reconcile.add_argument("intent_id")
    legacy_resolve = commands.add_parser(
        "legacy-resolve",
        help="release one retained v1 partial or unknown fence with signed owner resolution",
    )
    legacy_resolve.add_argument("intent_id")
    legacy_resolve.add_argument("resolution", choices=("accepted_as_is", "superseded"))
    operation_show = commands.add_parser(
        "operation-show", help="render one exact operation manifest"
    )
    operation_show.add_argument("operation_id")
    operation_reconcile = commands.add_parser(
        "operation-reconcile",
        help="force read-back for one pending operation without replay",
    )
    operation_reconcile.add_argument("operation_id")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    arguments = sys.argv[1:] if argv is None else argv
    if any(
        argument in {"--secret", "--key"}
        or argument.startswith(("--secret=", "--key="))
        for argument in arguments
    ):
        parser.error("Enter the receiver credential in a private terminal.")
    if arguments[:2] == ["routines", "setup"] and any(
        argument == "--url" or argument.startswith("--url=")
        for argument in arguments[2:]
    ):
        parser.error("Routines setup reads the receiver URL in a private terminal.")
    args = parser.parse_args(arguments)
    try:
        _dispatch(parser, args)
    except ConfigError as error:
        parser.error(str(error))


def _dispatch(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
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
            print(
                "The next request uses these preferences. No server restart is needed."
            )
        return
    if args.action == "doctor":
        _doctor(parser, wait=args.wait, public_url=args.public_url)
        return
    if args.action == "cloud-check":
        check = collect_cloud_check()
        print(json.dumps(check.as_dict(), sort_keys=True))
        if check.status != "ok":
            raise SystemExit(1)
        return
    if args.action == "support-bundle":
        print(collect_support_report().to_json(), end="")
        return
    if args.action == "skill-path":
        print(skill_path())
        return
    if args.action == "service":
        dry_run = getattr(args, "dry_run", False)
        try:
            result = service_action(args.service_action, dry_run=dry_run)
        except ServiceApplyError as error:
            parser.error(str(error))
            return
        except OSError as error:
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
    if args.action == "routines":
        _routines_command(parser, args)
        return
    if args.action == "owner-factor":
        _owner_factor(parser)
        return
    if args.action == "migration-report":
        _migration_report(parser)
        return
    if args.action == "legacy-reconcile":
        print(
            json.dumps(
                _workspace(parser).host_reconcile_v1_pending(args.intent_id),
                sort_keys=True,
            )
        )
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
    try:
        credentials = load_credentials()
    except ConfigError:
        parser.error(_LOGIN)
        return
    if args.action == "serve":
        _compose_server(
            parser, credentials, RoutineHTTPComposition.disabled()
        ).run()
        return
    bearer = credentials.bearer
    if bearer is None:
        parser.error("serve-http needs mcp_token from login")
    routines = _routine_http_composition(
        credentials, service_managed=bool(args.service_managed)
    )
    _compose_server(parser, credentials, routines).run_http(
        port=args.port, token=bearer.reveal()
    )


def _routines_command(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
    try:
        credentials = load_credentials()
    except ConfigError:
        parser.error(_LOGIN)
        return
    action = args.routines_action
    if action in {"configure", "setup"}:
        with _routine_secret_tty(parser) as terminal:
            if action == "setup":
                _write_routines_setup_guidance(terminal, receiver=args.receiver)
            receiver_url = getattr(args, "url", None) or getpass(
                _receiver_url_prompt(args.receiver, guided=action == "setup"),
                stream=terminal,
            )
            credential_label = (
                "Grok Bot webhook key"
                if args.receiver == "grok" and action == "setup"
                else (
                    "Grok webhook key"
                    if args.receiver == "grok"
                    else "Hermes webhook secret"
                )
            )
            secret = getpass(f"{credential_label}: ", stream=terminal)
            confirm = getpass(f"Confirm {credential_label}: ", stream=terminal)
        if secret != confirm:
            parser.error("webhook credential confirmation did not match")
        configured = configure_routines(
            email=credentials.email,
            receiver_kind=args.receiver,
            receiver_url=receiver_url,
            receiver_secret=ReceiverSecret(secret),
            poll_interval_seconds=args.interval,
            settle_seconds=args.settle,
        )
        if action == "setup":
            enabled = set_routines_enabled(True, email=credentials.email)
            try:
                service = service_action("install", dry_run=False)
            except (ConfigError, OSError, ServiceApplyError):
                parser.error(
                    "Routines are enabled, but the supervised service install failed. "
                    "Fix the service problem, then run "
                    "`things-orchestrator service install`. The saved receiver "
                    "values do not need to be entered again."
                )
                return
            print(
                json.dumps(
                    routines_status(enabled, email=credentials.email), sort_keys=True
                )
            )
            print(f"supervised service: {service.status.value}")
            print("Receiver instruction:")
            print(ROUTINE_RECEIVER_INSTRUCTION)
            print("Next readiness check:")
            print("things-orchestrator routines status")
            print("Wait for trigger_ready=true before the smoke test.")
            if args.receiver == "grok":
                print("Then turn the saved Grok Routine Active.")
            _print_routines_smoke_test()
            return
        print(
            json.dumps(
                routines_status(configured, email=credentials.email), sort_keys=True
            )
        )
        print(
            f"Stored disabled routines profile in {routines_config_path()} (mode 0600)."
        )
        print("Receiver instruction:")
        print(ROUTINE_RECEIVER_INSTRUCTION)
        print()
        print("Enable routines and restart the supervised service:")
        print("things-orchestrator routines enable")
        print("things-orchestrator service install")
        if args.receiver == "grok":
            print("Keep the saved Grok Routine inactive until trigger_ready=true.")
        return
    if action == "status":
        routine_service_state = collect_service_state()
        bearer = credentials.bearer
        runtime = (
            probe_routine_runtime(bearer.reveal())
            if bearer is not None
            else None
        )
        print(
            json.dumps(
                collect_routines_diagnostic(
                    credentials,
                    service_state=routine_service_state,
                    runtime=runtime,
                ).as_dict(),
                sort_keys=True,
            )
        )
        return
    enabled = action == "enable"
    config = set_routines_enabled(enabled, email=credentials.email)
    print(json.dumps(routines_status(config, email=credentials.email), sort_keys=True))
    if enabled:
        print("Restart required: things-orchestrator service install")
    else:
        print("A running worker will stop within one polling interval.")


def _receiver_url_prompt(receiver: str, *, guided: bool) -> str:
    if receiver == "grok":
        return "Grok Bot webhook POST URL: " if guided else "Grok webhook URL: "
    return "Hermes webhook URL: "


def _write_routines_setup_guidance(terminal: TextIO, *, receiver: str) -> None:
    terminal.write(
        f"In Things, create a tag titled exactly {ROUTINE_TRIGGER_TAG} if it does not "
        "already exist, then let Things sync. Leave it unassigned until the smoke "
        "test. Readiness waits for this tag to be discovered.\n\n"
    )
    if receiver == "grok":
        terminal.write(
            "First connect Grok to Things MCP. In a private terminal, run "
            "`things-orchestrator print-config --client grok --show-secrets`. "
            "At grok.com/connectors, choose New Connector, then Custom. Provide "
            "the HTTPS MCP URL and required authentication from the output. xAI "
            "requires the URL to be reachable from the public internet. The command "
            "rejects known local or private addresses but cannot verify reachability. "
            "Confirm that the connector exposes exactly eight tools, including "
            "things_get.\n\n"
            "In Grok Bot, create or edit a Routine, choose \"When a webhook fires\", "
            "paste the receiver instruction below, and save it before copying the "
            "generated POST URL and key.\n"
            "Keep the Grok Routine inactive until `things-orchestrator routines status` "
            "reports `trigger_ready=true`. Do not put the URL or key in argv or chat.\n\n"
            f"{ROUTINE_RECEIVER_INSTRUCTION}\n\n"
        )
        return
    prompt = ROUTINE_RECEIVER_INSTRUCTION + "\n\nAuthenticated event metadata:\n{__raw__}"
    terminal.write(
        "On the Hermes host, run `hermes gateway setup` and enable webhooks. "
        "Connect Things first with `things-orchestrator print-config --client "
        "hermes --show-secrets`. The configured server named things creates the "
        "mcp-things toolset. Use this receiver instruction:\n\n"
        f"{ROUTINE_RECEIVER_INSTRUCTION}\n\n"
        "Then create the route with this command:\n\n"
        "hermes webhook subscribe things-ai-task-created "
        f"--events {ROUTINE_EVENT_TYPE} "
        f"--prompt {shlex.quote(prompt)} "
        "--description 'Run the built-in Things AI task routine'\n\n"
        "The subscribe command prints the webhook URL and HMAC secret. Before you "
        "return to this prompt, edit ~/.hermes/webhook_subscriptions.json. In the "
        "things-ai-task-created entry, add \"toolsets\": [\"mcp-things\"]. The "
        "subscribe command cannot set route toolsets, and webhook runs otherwise "
        "use a restricted default. Inspect that entry and verify its exact "
        "\"toolsets\": [\"mcp-things\"] value. This file check does not prove MCP "
        "access. The positive selected-task smoke test does. Anyone with the route's "
        "HMAC secret then gains the eight bounded Things tools, so keep it private. "
        "Enter the URL and secret below. Do not put either value in argv or chat.\n"
    )


def _print_routines_smoke_test() -> None:
    print("Smoke test:")
    print("1. Record the current delivered count in routines status.")
    print("2. Create a fresh untagged task. Stop editing it and confirm no new delivery.")
    print(
        "3. Create another fresh task and assign the exact "
        f"{ROUTINE_TRIGGER_TAG} tag directly."
    )
    print("4. Stop editing it, wait for settlement and polling, then run:")
    print("things-orchestrator routines status")
    print("Confirm exactly one new delivery and the receiver action on only that task.")


def _login(
    parser: argparse.ArgumentParser,
    *,
    public_url: str,
    rotate_token: bool,
    timezone_name: str | None,
    show_secrets: bool,
) -> None:
    if not sys.stdin.isatty():
        parser.error(
            "login needs an interactive terminal. Do not paste the password into chat."
        )
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
    try:
        existing_url = load_mcp_url(
            preferences_file=preferences_file,
            credentials_file=creds,
        )
        legacy_url = (
            None
            if public_url.strip() or existing_url is not None
            else load_legacy_mcp_url(path=creds.with_name("mcp.http.json"))
        )
        mcp_url = select_login_mcp_url(
            explicit=public_url,
            saved=existing_url,
            legacy=legacy_url,
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
    print(
        "Next: install the HTTP service, run doctor --wait, then render a client config."
    )
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
    if kind is ClientKind.HERMES and show_secrets:
        print(
            "MCP bearer for the private Hermes prompt "
            f"(do not paste this into a shell): {credentials.bearer.reveal()}",
            file=sys.stderr,
        )
    print(rendered.body, end="")
    if rendered.secondary_body is not None:
        print("Alternative JSON:", file=sys.stderr)
        print(rendered.secondary_body, end="")
    if not show_secrets and kind is not ClientKind.CADDY:
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


def _doctor(parser: argparse.ArgumentParser, *, wait: bool, public_url: str) -> None:
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
        if stored_url is not None and stored_url not in targets:
            targets.append(stored_url)
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


def _server(
    parser: argparse.ArgumentParser,
    *,
    credentials: Credentials | None = None,
    routines: RoutineHTTPComposition | None = None,
) -> ThingsMCPServer:
    workspace = _workspace(parser, credentials=credentials)
    return ThingsMCPServer(workspace, routines=routines)


def _compose_server(
    parser: argparse.ArgumentParser,
    credentials: Credentials,
    routines: RoutineHTTPComposition,
) -> ThingsMCPServer:
    return _server(parser, credentials=credentials, routines=routines)


def _workspace(
    parser: argparse.ArgumentParser, *, credentials: Credentials | None = None
) -> ThingsWorkspace:
    if credentials is None:
        try:
            credentials = load_credentials()
        except ConfigError:
            parser.error(_LOGIN)
    email = credentials.email
    library = CloudLibrary(CloudClient(email, credentials.password))
    timezone_name = load_timezone()
    try:
        timezone = (
            ZoneInfo(timezone_name)
            if timezone_name
            else datetime.now().astimezone().tzinfo
        )
    except ZoneInfoNotFoundError:
        parser.error("Stored timezone is invalid. Run login --timezone Europe/Berlin.")

    def clock() -> datetime:
        return datetime.now(timezone)

    account_journal = journal_path(email)
    journal = SQLiteJournal(account_journal)
    journal.cutover_v1()
    journal.prune_v2(now=clock().isoformat(), retention_days=7)
    return ThingsWorkspace(
        library,
        journal=journal,
        clock=clock,
        account_id=email,
        preferences=load_preferences,
    )


def _routine_http_composition(
    credentials: Credentials, *, service_managed: bool
) -> RoutineHTTPComposition:
    if credentials.bearer is None or not service_managed:
        return RoutineHTTPComposition.disabled()
    try:
        config = load_routines_config()
    except ConfigError:
        return RoutineHTTPComposition.disabled()
    if (
        not isinstance(config, EnabledRoutineConfig)
        or config.profile.host_profile != "always_on"
        or config.profile.account_digest != account_digest(credentials.email)
    ):
        return RoutineHTTPComposition.disabled()
    profile = config.profile

    def create() -> RoutineWorker:
        return RoutineWorker(
            email=credentials.email,
            profile=profile,
            cloud=CloudClient(credentials.email, credentials.password),
            store=RoutineStore(profile),
            webhook=build_webhook(profile.receiver),
        )

    return RoutineHTTPComposition.enabled(create)


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


@contextmanager
def _routine_secret_tty(parser: argparse.ArgumentParser) -> Iterator[TextIO]:
    if sys.stdin.isatty() and sys.stderr.isatty() and sys.stderr.writable():
        yield sys.stderr
        return
    try:
        terminal = open("/dev/tty", "r+", encoding="utf-8")
    except OSError:
        parser.error("Routines configuration needs a private terminal.")
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
    if not workspace.host_resolve_legacy_v1(
        intent_id, cast(Any, resolution), authorization
    ):
        parser.error("retained v1 operation cannot be resolved")
    print(f"legacy_resolved: {resolution}")


def _local_timezone_name() -> str:
    timezone = datetime.now().astimezone().tzinfo
    key = getattr(timezone, "key", None)
    return str(key) if key else "UTC"


def _login_timezone(parser: argparse.ArgumentParser, explicit: str | None) -> str:
    candidate = explicit or _local_timezone_name()
    if explicit is None and candidate == "UTC":
        candidate = input("Owner timezone (IANA, for example Europe/Berlin): ").strip()
    try:
        ZoneInfo(candidate)
    except ZoneInfoNotFoundError:
        parser.error("--timezone needs an IANA name such as Europe/Berlin")
    return candidate


if __name__ == "__main__":
    main()
