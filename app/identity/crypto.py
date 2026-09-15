"""Cryptographic primitives for agent identity.

All primitives come from the ``cryptography`` library - nothing here is
hand-rolled. This module is the ONLY place that touches Ed25519 or AES-GCM.

Design:

    Ed25519 keypair                 (signing identity)
    agent_id = "nexus:ed25519:" + SHA-256(public_key)[:16 hex]
                                    (deterministic, verifiable)
    private key at rest:
        AES-256-GCM(secret, plaintext)
        stored as base64(nonce || ciphertext+tag)

The AES key is derived from the deployment secret (NEXUS_IDENTITY_KEY) with
SHA-256 (the secret is high-entropy, so a plain hash is a sufficient KDF;
scrypt would add cost without benefit here - no low-entropy passwords).
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_ALGORITHM = "Ed25519"
AGENT_ID_PREFIX = "nexus:ed25519:"
# 16 bytes of the public-key hash = 32 hex chars: collision-resistant for
# this purpose while keeping IDs readable.
AGENT_ID_HASH_BYTES = 16
AES_NONCE_BYTES = 12
FINGERPRINT_GROUPS = 8


class IdentityCryptoError(Exception):
    """Raised when key generation, encryption, or decryption fails."""


class SignatureVerificationError(Exception):
    """Raised when a signature does not verify."""


# --- Keypair -----------------------------------------------------------------


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key()


def public_key_bytes(public_key: Ed25519PublicKey) -> bytes:
    """Raw 32-byte public key."""
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def private_key_bytes(private_key: Ed25519PrivateKey) -> bytes:
    """Raw 32-byte private key (never leaves this package unencrypted)."""
    return private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def load_public_key(raw: bytes) -> Ed25519PublicKey:
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except Exception as exc:
        raise IdentityCryptoError("Invalid public key bytes.") from exc


def load_private_key(raw: bytes) -> Ed25519PrivateKey:
    try:
        return Ed25519PrivateKey.from_private_bytes(raw)
    except Exception as exc:
        raise IdentityCryptoError("Invalid private key bytes.") from exc


# --- Agent ID ------------------------------------------------------------------


def agent_id_from_public_key(public_key_raw: bytes) -> str:
    """Deterministic, verifiable agent identifier.

    Format: ``nexus:ed25519:<first 16 bytes of SHA-256(raw public key) as hex>``

    Anyone holding the public key can recompute this ID and confirm the key
    really belongs to the claimed agent. The private key is never involved.
    """
    digest = hashlib.sha256(public_key_raw).hexdigest()
    return f"{AGENT_ID_PREFIX}{digest[: AGENT_ID_HASH_BYTES * 2]}"


def fingerprint_from_public_key(public_key_raw: bytes) -> str:
    """Human-readable fingerprint: SHA-256 of the raw key, grouped hex.

    Format: ``AB12-CD34-...`` (8 groups of 4 hex chars, uppercase).
    """
    digest = hashlib.sha256(public_key_raw).hexdigest().upper()
    groups = [
        digest[i : i + 4] for i in range(0, FINGERPRINT_GROUPS * 4, 4)
    ]
    return "-".join(groups)


# --- Signing / verification ------------------------------------------------------


def sign_bytes(private_key: Ed25519PrivateKey, data: bytes) -> bytes:
    return private_key.sign(data)


def verify_bytes(
    public_key: Ed25519PublicKey, data: bytes, signature: bytes
) -> bool:
    try:
        public_key.verify(signature, data)
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False


# --- Private-key encryption at rest -----------------------------------------------


def _derive_aes_key(secret: str) -> bytes:
    """SHA-256 of the high-entropy deployment secret -> 256-bit AES key."""
    return hashlib.sha256(secret.encode("utf-8")).digest()


def encrypt_private_key(private_key_raw: bytes, secret: str) -> str:
    """AES-256-GCM. Returns base64(nonce[12] || ciphertext+tag).

    The nonce is random per encryption; authenticity is provided by the GCM
    tag, so tampering with the stored value fails decryption loudly.
    """
    if not secret:
        raise IdentityCryptoError("Identity encryption secret is empty.")
    aes_key = _derive_aes_key(secret)
    nonce = os.urandom(AES_NONCE_BYTES)
    aesgcm = AESGCM(aes_key)
    ciphertext = aesgcm.encrypt(nonce, private_key_raw, associated_data=None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_private_key(encrypted: str, secret: str) -> bytes:
    """Inverse of :func:`encrypt_private_key`.

    Raises:
        IdentityCryptoError: wrong secret, corrupted data, or bad base64.
    """
    if not secret:
        raise IdentityCryptoError("Identity encryption secret is empty.")
    try:
        blob = base64.b64decode(encrypted.encode("ascii"))
        nonce, ciphertext = blob[:AES_NONCE_BYTES], blob[AES_NONCE_BYTES:]
        aesgcm = AESGCM(_derive_aes_key(secret))
        return aesgcm.decrypt(nonce, ciphertext, associated_data=None)
    except (InvalidTag, ValueError, TypeError) as exc:
        raise IdentityCryptoError(
            "Could not decrypt the agent private key: the "
            "NEXUS_IDENTITY_KEY secret is wrong, or the stored key "
            "material is corrupted."
        ) from exc


def keypair_matches(private_key: Ed25519PrivateKey, public_key: Ed25519PublicKey) -> bool:
    """True when the private key derives exactly this public key."""
    return public_key_bytes(private_key.public_key()) == public_key_bytes(public_key)


__all__ = [
    "AGENT_ID_HASH_BYTES",
    "AGENT_ID_PREFIX",
    "KEY_ALGORITHM",
    "IdentityCryptoError",
    "SignatureVerificationError",
    "agent_id_from_public_key",
    "decrypt_private_key",
    "encrypt_private_key",
    "fingerprint_from_public_key",
    "generate_keypair",
    "keypair_matches",
    "load_private_key",
    "load_public_key",
    "private_key_bytes",
    "public_key_bytes",
    "sign_bytes",
    "verify_bytes",
]
