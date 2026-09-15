"""Unit tests for identity cryptography and canonical serialization.

Pure in-memory; no database, no network, no real secrets.
"""

from __future__ import annotations

import base64

import pytest

from app.identity import crypto
from app.identity.serialization import (
    CanonicalizationError,
    canonical_json_bytes,
)


# --- Keypair generation --------------------------------------------------------


def test_generate_keypair_produces_raw_32_byte_keys() -> None:
    private_key, public_key = crypto.generate_keypair()
    assert len(crypto.private_key_bytes(private_key)) == 32
    assert len(crypto.public_key_bytes(public_key)) == 32
    # The private key derives exactly this public key.
    assert crypto.keypair_matches(private_key, public_key)


def test_generated_keypairs_are_unique() -> None:
    first = crypto.generate_keypair()
    second = crypto.generate_keypair()
    assert crypto.public_key_bytes(first[1]) != crypto.public_key_bytes(second[1])


def test_load_public_key_rejects_garbage() -> None:
    with pytest.raises(crypto.IdentityCryptoError):
        crypto.load_public_key(b"not-a-key")


def test_load_private_key_rejects_garbage() -> None:
    with pytest.raises(crypto.IdentityCryptoError):
        crypto.load_private_key(b"still-not-a-key")


# --- Agent ID ------------------------------------------------------------------


def test_agent_id_is_deterministic_from_public_key() -> None:
    _, public_key = crypto.generate_keypair()
    raw = crypto.public_key_bytes(public_key)
    assert crypto.agent_id_from_public_key(raw) == crypto.agent_id_from_public_key(raw)


def test_different_keys_yield_different_agent_ids() -> None:
    key_a = crypto.public_key_bytes(crypto.generate_keypair()[1])
    key_b = crypto.public_key_bytes(crypto.generate_keypair()[1])
    assert crypto.agent_id_from_public_key(key_a) != crypto.agent_id_from_public_key(key_b)


def test_agent_id_format() -> None:
    _, public_key = crypto.generate_keypair()
    agent_id = crypto.agent_id_from_public_key(crypto.public_key_bytes(public_key))
    prefix, algorithm, fingerprint = agent_id.split(":")
    assert prefix == "nexus"
    assert algorithm == "ed25519"
    assert len(fingerprint) == 32  # 16 bytes hex
    int(fingerprint, 16)  # must be valid hex


def test_fingerprint_format() -> None:
    _, public_key = crypto.generate_keypair()
    fingerprint = crypto.fingerprint_from_public_key(
        crypto.public_key_bytes(public_key)
    )
    groups = fingerprint.split("-")
    assert len(groups) == 8
    for group in groups:
        assert len(group) == 4
        int(group, 16)  # valid uppercase hex


# --- Signing / verification -------------------------------------------------------


def test_sign_then_verify_roundtrip() -> None:
    private_key, public_key = crypto.generate_keypair()
    message = b"Hello from Nexus"
    signature = crypto.sign_bytes(private_key, message)
    assert crypto.verify_bytes(public_key, message, signature) is True


def test_tampered_message_fails_verification() -> None:
    private_key, public_key = crypto.generate_keypair()
    signature = crypto.sign_bytes(private_key, b"Hello from Nexus")
    assert crypto.verify_bytes(public_key, b"Hello from Nexus!", signature) is False


def test_wrong_public_key_fails_verification() -> None:
    private_key, _ = crypto.generate_keypair()
    other_public = crypto.generate_keypair()[1]
    signature = crypto.sign_bytes(private_key, b"Hello from Nexus")
    assert crypto.verify_bytes(other_public, b"Hello from Nexus", signature) is False


def test_corrupted_signature_fails_verification() -> None:
    private_key, public_key = crypto.generate_keypair()
    signature = bytearray(crypto.sign_bytes(private_key, b"msg"))
    signature[0] ^= 0xFF
    assert crypto.verify_bytes(public_key, b"msg", bytes(signature)) is False


# --- Private-key encryption at rest ---------------------------------------------------


SECRET = "test-only-secret-not-a-real-one-0123456789"


def test_encrypted_private_key_is_not_plaintext() -> None:
    private_key, _ = crypto.generate_keypair()
    raw = crypto.private_key_bytes(private_key)
    encrypted = crypto.encrypt_private_key(raw, SECRET)

    assert encrypted != raw.hex()
    assert raw not in base64.b64decode(encrypted)
    assert raw.hex() not in encrypted


def test_encrypt_decrypt_roundtrip() -> None:
    private_key, _ = crypto.generate_keypair()
    raw = crypto.private_key_bytes(private_key)
    encrypted = crypto.encrypt_private_key(raw, SECRET)
    assert crypto.decrypt_private_key(encrypted, SECRET) == raw


def test_wrong_secret_fails_decryption() -> None:
    private_key, _ = crypto.generate_keypair()
    encrypted = crypto.encrypt_private_key(
        crypto.private_key_bytes(private_key), SECRET
    )
    with pytest.raises(crypto.IdentityCryptoError):
        crypto.decrypt_private_key(encrypted, "a-completely-different-secret")


def test_corrupted_ciphertext_fails_decryption() -> None:
    private_key, _ = crypto.generate_keypair()
    encrypted = crypto.encrypt_private_key(
        crypto.private_key_bytes(private_key), SECRET
    )
    blob = bytearray(base64.b64decode(encrypted))
    blob[-1] ^= 0xFF  # flip a bit inside the GCM tag
    corrupted = base64.b64encode(bytes(blob)).decode()
    with pytest.raises(crypto.IdentityCryptoError):
        crypto.decrypt_private_key(corrupted, SECRET)


def test_encryption_is_randomised_per_call() -> None:
    private_key, _ = crypto.generate_keypair()
    raw = crypto.private_key_bytes(private_key)
    assert crypto.encrypt_private_key(raw, SECRET) != crypto.encrypt_private_key(raw, SECRET)


def test_empty_secret_rejected() -> None:
    with pytest.raises(crypto.IdentityCryptoError):
        crypto.encrypt_private_key(b"x" * 32, "")
    with pytest.raises(crypto.IdentityCryptoError):
        crypto.decrypt_private_key("anything", "")


# --- Canonical serialization -------------------------------------------------------


def test_canonical_json_is_deterministic() -> None:
    payload_a = {"b": 2, "a": 1, "nested": {"z": True, "y": None}}
    payload_b = {"a": 1, "b": 2, "nested": {"y": None, "z": True}}
    assert canonical_json_bytes(payload_a) == canonical_json_bytes(payload_b)


def test_canonical_json_is_compact_utf8() -> None:
    data = canonical_json_bytes({"key": "café"})
    assert data == b'{"key":"caf\xc3\xa9"}'
    assert b" " not in data


def test_canonical_json_rejects_floats() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes({"price": 10.5})
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes([1.0])


def test_canonical_json_rejects_non_json_types() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes({"when": object()})
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes(float("nan"))


def test_canonical_signing_roundtrip() -> None:
    """Sign canonical bytes, verify against re-serialized identical payload."""
    private_key, public_key = crypto.generate_keypair()
    payload = {"to": "agent-b", "body": "hello", "n": 42}
    signature = crypto.sign_bytes(private_key, canonical_json_bytes(payload))
    # Independent re-serialization of an "equal" payload verifies.
    assert crypto.verify_bytes(public_key, canonical_json_bytes(payload), signature)
