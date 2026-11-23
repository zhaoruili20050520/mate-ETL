"""Tests for settings normalization of the capability signing key."""

from __future__ import annotations

from etl_agent.settings import Settings

_ESCAPED_PEM = "-----BEGIN PRIVATE KEY-----\\nMIIB\\n-----END PRIVATE KEY-----\\n"


def test_capability_pem_escaped_newlines_are_normalized() -> None:
    """A single-line PEM with ``\\n`` escapes must become a real PEM."""
    settings = Settings(CAPABILITY_PRIVATE_KEY_PEM=_ESCAPED_PEM)

    assert "\n" in settings.capability_private_key_pem
    assert "\\n" not in settings.capability_private_key_pem
    assert settings.capability_private_key_pem.startswith("-----BEGIN PRIVATE KEY-----")


def test_capability_pem_with_real_newlines_is_kept_as_is() -> None:
    """A PEM with real newlines must not be modified."""
    pem = "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----\n"
    settings = Settings(CAPABILITY_PRIVATE_KEY_PEM=pem)

    assert settings.capability_private_key_pem == pem


def test_capability_pem_default_is_empty() -> None:
    """Without configuration the key stays empty (per-process keypair)."""
    settings = Settings(CAPABILITY_PRIVATE_KEY_PEM="")

    assert settings.capability_private_key_pem == ""
