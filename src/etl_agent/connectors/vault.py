"""Vault KV v2 client wrapper.

Security principle: all database credentials (passwords and
usernames) are stored in HashiCorp Vault under the KV v2 secrets
engine.  The application database stores **only** a ``secret_ref``
path pointing to the Vault entry -- never plaintext credentials.

This module implements **only** KV v2 operations, as specified.
Other Vault secret engines (Transit, Database, PKI, etc.) are out
of scope.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, cast

import hvac  # type: ignore[import-untyped]
from hvac.exceptions import InvalidPath, VaultError  # type: ignore[import-untyped]

from etl_agent.settings import settings

logger = logging.getLogger(__name__)

# Sub-path under the KV v2 mount for data-source credentials.
_DS_PREFIX = "ds"


class VaultClient:
    """Thin wrapper around ``hvac.Client`` for KV v2 operations.

    The client is configured from ``settings`` (VAULT_ADDR,
    VAULT_TOKEN, VAULT_SECRET_PATH).  The KV v2 mount point is
    ``settings.vault_secret_path`` (default ``etl-agent``).
    """

    def __init__(self) -> None:
        self._client = hvac.Client(
            url=settings.vault_addr,
            token=settings.vault_token,
            timeout=10,
        )
        self._mount_point = settings.vault_secret_path
        self._ensure_kv_mount()

    # ------------------------------------------------------------------
    # Mount management
    # ------------------------------------------------------------------

    def _ensure_kv_mount(self) -> None:
        """Ensure the KV v2 mount exists; create it if missing.

        Idempotent: checks ``list_mount_secrets_engines`` first.
        """
        try:
            mounts: dict[str, Any] = self._client.sys.list_mounted_secrets_engines()
        except VaultError:
            mounts = {}

        # hvac uses trailing slashes in mount keys, e.g. "etl-agent/"
        mount_key = self._mount_point.rstrip("/") + "/"
        if mount_key not in mounts and self._mount_point not in mounts:
            self._client.sys.enable_secrets_engine(
                backend_type="kv",
                path=self._mount_point,
                options={"version": "2"},
            )
            logger.info("Enabled KV v2 secrets engine at mount '%s'", self._mount_point)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @staticmethod
    def build_secret_path(connection_id: uuid.UUID) -> str:
        """Return the KV v2 path for a data-source connection.

        Format: ``ds/{connection_id}``
        This path is stored in ``data_source_connections.secret_ref``.
        """
        return f"{_DS_PREFIX}/{connection_id}"

    # ------------------------------------------------------------------
    # KV v2 CRUD
    # ------------------------------------------------------------------

    def write_secret(self, path: str, data: dict[str, Any]) -> str:
        """Write (create or update) a secret at the given path.

        Returns the full reference path (``{mount_point}/{path}``)
        suitable for storing in the database.
        """
        self._client.secrets.kv.v2.create_or_update_secret(
            path=path,
            secret=data,
            mount_point=self._mount_point,
        )
        ref = f"{self._mount_point}/{path}"
        logger.debug("Wrote secret to %s", ref)
        return ref

    def read_secret(self, path: str) -> dict[str, Any]:
        """Read the latest version of a secret and return its data.

        Raises ``KeyError`` if the path does not exist (InvalidPath).
        """
        try:
            response = self._client.secrets.kv.v2.read_secret(
                path=path,
                mount_point=self._mount_point,
                raise_on_deleted_version=True,
            )
        except InvalidPath as exc:
            raise KeyError(f"Secret not found at path '{path}'") from exc

        # KV v2 response structure:
        #   {"data": {"version": N, "data": {...actual secret...}}}
        return cast(dict[str, Any], response["data"]["data"])

    def read_secret_by_ref(self, secret_ref: str) -> dict[str, Any]:
        """Read credentials by their stored reference path.

        ``secret_ref`` format: ``{mount_point}/{path}``
        e.g. ``etl-agent/ds/abc-123``

        The mount prefix is stripped so the path can be resolved
        even if the configured mount point changes.
        """
        path = self._strip_mount_prefix(secret_ref)
        return self.read_secret(path)

    def delete_secret(self, path: str) -> None:
        """Permanently delete all versions and metadata for a secret."""
        try:
            self._client.secrets.kv.v2.delete_metadata_and_all_versions(
                path=path,
                mount_point=self._mount_point,
            )
        except InvalidPath:
            logger.warning("Secret path '%s' not found during deletion", path)

    def delete_secret_by_ref(self, secret_ref: str) -> None:
        """Delete credentials by their stored reference path."""
        path = self._strip_mount_prefix(secret_ref)
        self.delete_secret(path)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _strip_mount_prefix(self, secret_ref: str) -> str:
        """Remove the mount-point prefix from a full reference path."""
        prefix = f"{self._mount_point}/"
        if secret_ref.startswith(prefix):
            return secret_ref[len(prefix) :]
        return secret_ref

    @property
    def is_initialized(self) -> bool:
        """Return ``True`` if the Vault server responds."""
        try:
            return bool(self._client.sys.read_health_status())
        except VaultError:
            return False


# =====================================================================
# Module-level singleton
# =====================================================================

_vault_client: VaultClient | None = None


def get_vault_client() -> VaultClient:
    """Return the process-wide ``VaultClient`` singleton.

    The client is lazily initialised on first call.  Subsequent
    calls return the same instance.
    """
    global _vault_client
    if _vault_client is None:
        _vault_client = VaultClient()
    return _vault_client


def reset_vault_client() -> None:
    """Discard the cached client (mainly for testing)."""
    global _vault_client
    _vault_client = None


__all__ = [
    "VaultClient",
    "get_vault_client",
    "reset_vault_client",
]
