"""Derive browser recovery scope without exposing user IDs."""

import hashlib
import hmac

from ..database import get_meta

RECOVERY_SCOPE_SECRET_META_KEY = "assistant_recovery_scope_secret"


def derive_recovery_scope(db_path: str, user_id: str, *, domain: bytes) -> str:
    if not user_id:
        raise ValueError("user_id must be non-empty")
    if not domain or not domain.endswith(b"\0"):
        raise ValueError("recovery scope domain must be non-empty and terminated")
    secret = get_meta(db_path, RECOVERY_SCOPE_SECRET_META_KEY)
    if (
        secret is None
        or len(secret) != 64
        or any(character not in "0123456789abcdef" for character in secret)
    ):
        raise RuntimeError("recovery scope secret is missing or invalid")
    return hmac.new(
        bytes.fromhex(secret),
        domain + user_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
