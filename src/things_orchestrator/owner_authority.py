"""CLI-only owner signing and spoof-resistant operation rendering.

The MCP request path does not import this module or request the passphrase.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import tempfile
import unicodedata
from base64 import b64decode, b64encode
from hashlib import scrypt, sha256
from pathlib import Path
from secrets import token_bytes

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .journal import (
    OwnerAuthorization,
    V2Operation,
    owner_authorization_binding_json,
    owner_operation_is_valid,
    owner_public_key_path,
)

_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)?)")


def owner_factor_path() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME")
    base = Path(root) if root else Path.home() / ".config"
    return base / "things-orchestrator" / "owner-factor.json"


def enroll_owner_factor(passphrase: str, *, path: Path | None = None) -> Path:
    if len(passphrase) < 12:
        raise ValueError("owner passphrase needs at least 12 characters")
    salt = token_bytes(16)
    private_key = Ed25519PrivateKey.generate()
    encrypted_private_key = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(passphrase.encode()),
    )
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    payload = {
        "version": 1,
        "salt": b64encode(salt).decode(),
        "verifier": b64encode(_digest(passphrase, salt)).decode(),
        "encrypted_private_key": b64encode(encrypted_private_key).decode(),
    }
    target = path or owner_factor_path()
    _atomic_private_write(
        target,
        (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode(),
    )
    public_target = (
        target.with_name("owner-public-key.ed25519")
        if path is not None
        else owner_public_key_path()
    )
    _atomic_private_write(public_target, public_key)
    return target


def verify_owner_factor(passphrase: str, *, path: Path | None = None) -> bool:
    target = path or owner_factor_path()
    payload = json.loads(target.read_text())
    if payload.get("version") != 1:
        raise ValueError("unsupported owner factor version")
    salt = b64decode(payload["salt"], validate=True)
    verifier = b64decode(payload["verifier"], validate=True)
    return hmac.compare_digest(_digest(passphrase, salt), verifier)


def authorization_binding(operation: V2Operation, *, action: str) -> str:
    canonical = owner_authorization_binding_json(operation, action=action)
    return "sha256:v1:" + sha256(canonical.encode()).hexdigest()


def verified_authorization(
    operation: V2Operation,
    *,
    action: str,
    passphrase: str,
    path: Path | None = None,
) -> OwnerAuthorization | None:
    """Return a sealed authorization only after host-factor verification."""

    target = path or owner_factor_path()
    if not verify_owner_factor(passphrase, path=target):
        return None
    payload = json.loads(target.read_text())
    encrypted = b64decode(payload["encrypted_private_key"], validate=True)
    private_key = serialization.load_pem_private_key(
        encrypted,
        password=passphrase.encode(),
    )
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("owner factor key type is invalid")
    binding = owner_authorization_binding_json(operation, action=action)
    return OwnerAuthorization(
        binding_json=binding,
        signature=b64encode(private_key.sign(binding.encode())).decode(),
    )


def render_operation(operation: V2Operation) -> str:
    if not owner_operation_is_valid(operation):
        raise ValueError("operation manifest failed its integrity check")
    lines = [
        f"operation_id | {host_escape(operation.operation_id)}",
        f"state | {operation.state}",
        f"action | {host_escape(operation.tool)}",
    ]
    writes = operation.manifest.get("writes", [])
    display_titles = operation.manifest.get("display_titles", [])
    if isinstance(writes, list):
        for index, write in enumerate(writes, start=1):
            if not isinstance(write, dict):
                continue
            action = host_escape(str(write.get("action", "unknown")))
            kind = host_escape(str(write.get("kind", "item")))
            uuid = host_escape(str(write.get("uuid", "unknown")))
            lines.append(f"manifest[{index}] | {action} | {kind}:{uuid}")
            if isinstance(display_titles, list) and index <= len(display_titles):
                lines.append(f"  title | {host_escape(str(display_titles[index - 1]))}")
            for field, value in sorted(write.items()):
                if field in {"action", "kind", "uuid"} or value is None:
                    continue
                lines.append(f"  {host_escape(str(field))} | {host_escape(str(value))}")
    legacy_plan = operation.manifest.get("legacy_plan")
    if isinstance(legacy_plan, dict):
        canonical_plan = json.dumps(
            legacy_plan, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        lines.append(f"legacy_plan | {host_escape(canonical_plan)}")
    lines.append("preserves | every omitted field and member")
    if operation.tool == "things_trash":
        lines.append("warning | moves exact items to recoverable Trash")
    return "\n".join(lines)


def host_escape(value: str) -> str:
    cleaned = _ANSI.sub("", value)
    pieces: list[str] = []
    for character in cleaned:
        code = ord(character)
        if (
            character in {"\n", "\r", "\t", "|", "\\"}
            or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        ):
            pieces.append(f"\\u{code:04x}")
        else:
            pieces.append(character)
    return "".join(pieces)


def _digest(passphrase: str, salt: bytes) -> bytes:
    return scrypt(passphrase.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


def _atomic_private_write(target: Path, body: bytes) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    target.chmod(0o600)
