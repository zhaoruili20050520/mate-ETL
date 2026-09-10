"""Capability token issuance and verification using Ed25519 signatures.

A **Capability** is a short-lived, single-use authorization token
that permits a consumer (e.g. the SeaTunnel job runner) to execute
a committed ETL pipeline.  It is:

- **Signed** with Ed25519 so the consumer can verify authenticity
  without sharing a secret.
- **Short-lived**: expires 5 minutes after issuance.
- **Single-use**: the ``capability_id`` is recorded in
  ``capability_logs``; a second consumption attempt is rejected
  by the primary-key constraint.
- **Replay-resistant**: the token includes a nonce (UUID4) and the
  execution_run_id, so even if intercepted it cannot be replayed
  for a different run.

Token format (URL-safe base64 of a JSON payload + signature):

    <payload_b64>.<signature_b64>

Payload (JSON):
    {
        "cap_id": "<uuid4>",
        "run_id": "<uuid>",
        "issued_at": "<iso8601>",
        "expires_at": "<iso8601>"
    }
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from etl_agent.settings import settings

# --- Module-level keypair (generated once per process) --------------------

_private_key: Ed25519PrivateKey | None = None
_public_key: Ed25519PublicKey | None = None

# Lifetime of a capability token
CAPABILITY_TTL_SECONDS = 300  # 5 minutes


def _get_private_key() -> Ed25519PrivateKey:
    """Return the process-level Ed25519 private key (lazy init)."""
    global _private_key  # noqa: PLW0603
    if _private_key is None:
        pem = settings.capability_private_key_pem
        if pem:
            load_private_key_pem(pem)
        else:
            _private_key = Ed25519PrivateKey.generate()
    if _private_key is None:
        _private_key = Ed25519PrivateKey.generate()
    return _private_key


def _get_public_key() -> Ed25519PublicKey:
    """Return the process-level Ed25519 public key (lazy init)."""
    global _public_key  # noqa: PLW0603
    if _public_key is None:
        _public_key = _get_private_key().public_key()
    return _public_key


def get_public_key_pem() -> str:
    """Return the public key in PEM format (for sharing with consumers)."""
    return (
        _get_public_key()
        .public_bytes(
            encoding=Encoding.PEM,
            format=PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )


def export_private_key_pem() -> str:
    """Return the private key in PEM format (for persistence)."""
    return (
        _get_private_key()
        .private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.PKCS8,
            encryption_algorithm=NoEncryption(),
        )
        .decode("utf-8")
    )


def load_private_key_pem(pem: str) -> None:
    """Load a private key from PEM (for restoring across restarts)."""
    global _private_key, _public_key  # noqa: PLW0603
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    _private_key = cast(
        Ed25519PrivateKey,
        load_pem_private_key(
            pem.encode("utf-8"),
            password=None,
        ),
    )
    _public_key = _private_key.public_key()


# --- Token data structures -----------------------------------------------


@dataclass(frozen=True)
class CapabilityPayload:
    """The decoded payload of a capability token."""

    cap_id: str
    run_id: str
    issued_at: datetime
    expires_at: datetime

    def to_dict(self) -> dict[str, str]:
        """Serialize to a JSON-compatible dict."""
        return {
            "cap_id": self.cap_id,
            "run_id": self.run_id,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> CapabilityPayload:
        """Deserialize from a JSON-compatible dict."""
        return cls(
            cap_id=d["cap_id"],
            run_id=d["run_id"],
            issued_at=datetime.fromisoformat(d["issued_at"]),
            expires_at=datetime.fromisoformat(d["expires_at"]),
        )


@dataclass(frozen=True)
class IssuedCapability:
    """A fully issued capability token + its decoded payload."""

    token: str  # <payload_b64>.<signature_b64>
    payload: CapabilityPayload


# --- Token issuance and verification -------------------------------------


def issue_capability(
    execution_run_id: uuid.UUID,
    ttl_seconds: int = CAPABILITY_TTL_SECONDS,
) -> IssuedCapability:
    """Issue a signed capability token for an execution run.

    Returns the serialized token and the decoded payload.
    """
    now = datetime.now(UTC)
    expires = now + timedelta(seconds=ttl_seconds)

    payload = CapabilityPayload(
        cap_id=str(uuid.uuid4()),
        run_id=str(execution_run_id),
        issued_at=now,
        expires_at=expires,
    )

    payload_json = json.dumps(payload.to_dict(), sort_keys=True)
    payload_b64 = base64.urlsafe_b64encode(
        payload_json.encode("utf-8"),
    ).decode("utf-8")

    signature = _get_private_key().sign(payload_b64.encode("utf-8"))
    sig_b64 = base64.urlsafe_b64encode(signature).decode("utf-8")

    token = f"{payload_b64}.{sig_b64}"
    return IssuedCapability(token=token, payload=payload)


def verify_capability(token: str) -> CapabilityPayload:
    """Verify a capability token's signature and return its payload.

    Raises ``ValueError`` if:
    - The token format is invalid.
    - The signature does not match.
    - The token has expired.
    """
    parts = token.split(".")
    if len(parts) != 2:
        raise ValueError("Invalid token format: expected '<payload>.<signature>'")

    payload_b64, sig_b64 = parts

    try:
        signature = base64.urlsafe_b64decode(sig_b64)
    except Exception as exc:
        raise ValueError(f"Invalid signature encoding: {exc}") from exc

    # Verify signature (raises if invalid)
    try:
        _get_public_key().verify(
            signature,
            payload_b64.encode("utf-8"),
        )
    except Exception as exc:
        raise ValueError(f"Signature verification failed: {exc}") from exc

    # Decode payload
    try:
        payload_json = base64.urlsafe_b64decode(payload_b64).decode("utf-8")
        payload_dict = json.loads(payload_json)
    except Exception as exc:
        raise ValueError(f"Invalid payload encoding: {exc}") from exc

    payload = CapabilityPayload.from_dict(payload_dict)

    # Check expiry
    now = datetime.now(UTC)
    if now > payload.expires_at:
        raise ValueError(
            f"Capability token expired at {payload.expires_at.isoformat()}",
        )

    return payload


__all__ = [
    "CAPABILITY_TTL_SECONDS",
    "CapabilityPayload",
    "IssuedCapability",
    "export_private_key_pem",
    "get_public_key_pem",
    "issue_capability",
    "load_private_key_pem",
    "verify_capability",
]
