from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta

ACCESS_SESSION_TTL = timedelta(minutes=15)
REFRESH_SESSION_TTL = timedelta(days=30)


def utc_now() -> datetime:
    return datetime.now(UTC)


def digest_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_email(value: str) -> str:
    return value.strip().casefold()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    password_digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return "$".join(
        (
            "scrypt",
            urlsafe_b64encode(salt).decode("ascii"),
            urlsafe_b64encode(password_digest).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, encoded_salt, encoded_digest = encoded.split("$", 2)
        if algorithm != "scrypt":
            return False
        salt = urlsafe_b64decode(encoded_salt.encode("ascii"))
        expected = urlsafe_b64decode(encoded_digest.encode("ascii"))
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=2**14,
            r=8,
            p=1,
            dklen=len(expected),
        )
        return hmac.compare_digest(expected, candidate)
    except (ValueError, TypeError):
        return False


def email_username(email: str) -> str:
    local_part = email.partition("@")[0]
    stem = re.sub(r"[^a-z0-9._-]+", "-", local_part.casefold()).strip("-._") or "user"
    suffix = digest_secret(email)[:8]
    return f"{stem[:60]}-{suffix}"
