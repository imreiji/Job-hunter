"""Password-based encryption for everything the public site serves.

GitHub Pages sites are public even when the repository is private, so the poller publishes
job data only as ciphertext. The page derives the same key from your password in the browser
(WebCrypto) and decrypts locally. Format (JSON):

    {"v": 1, "kdf": "PBKDF2-SHA256", "iter": 600000, "salt": b64, "iv": b64, "ct": b64}

AES-256-GCM authenticates the data, so a wrong password or a tampered file fails to decrypt.
The salt is stored in the repo (data/site_salt.txt) and reused, so a browser that remembers the
derived key keeps working across polls; every encryption uses a fresh random IV.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ITERATIONS = 600_000  # OWASP 2023 recommendation for PBKDF2-HMAC-SHA256
MIN_PASSWORD_LENGTH = 12


class DecryptionError(ValueError):
    pass


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def derive_key(password: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations, dklen=32)


def load_or_create_salt(path: Path) -> bytes:
    if path.exists():
        return base64.b64decode(path.read_text().strip())
    salt = os.urandom(16)
    path.parent.mkdir(exist_ok=True)
    path.write_text(_b64(salt) + "\n")
    return salt


def encrypt_json(obj, password: str, salt: bytes, key: bytes | None = None) -> dict:
    key = key or derive_key(password, salt)
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode(), None)
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": ITERATIONS, "salt": _b64(salt), "iv": _b64(iv), "ct": _b64(ct)}


def is_encrypted(blob) -> bool:
    return isinstance(blob, dict) and {"salt", "iv", "ct"} <= blob.keys()


def decrypt_json(blob: dict, password: str):
    try:
        salt, iv, ct = (base64.b64decode(blob[k]) for k in ("salt", "iv", "ct"))
        key = derive_key(password, salt, int(blob.get("iter", ITERATIONS)))
        return json.loads(AESGCM(key).decrypt(iv, ct, None))
    except Exception as e:  # InvalidTag, bad base64, bad JSON: all mean "can't trust this"
        raise DecryptionError("wrong password or corrupted file") from e
