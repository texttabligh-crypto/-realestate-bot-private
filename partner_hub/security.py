import hashlib
import os
from cryptography.fernet import Fernet


def _fernet() -> Fernet:
    key = os.environ.get("PARTNER_ENCRYPTION_KEY", "").encode()
    if not key:
        raise RuntimeError("PARTNER_ENCRYPTION_KEY is not configured")
    return Fernet(key)


def encrypt_token(token: str) -> str:
    return _fernet().encrypt(token.strip().encode()).decode()


def decrypt_token(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.strip().encode()).hexdigest()
