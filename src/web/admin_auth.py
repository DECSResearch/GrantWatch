"""Named accounts for the admin page.

GRANTS_ADMIN_USERS lists one ``name:scrypt$n$r$p$salt$hash`` entry per person,
separated by commas or newlines. Only the password hash is stored; make an
entry with ``python main.py admin-user NAME``. Standard library only, so the
command line can use it without the web framework.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from typing import Dict, Optional, Tuple

from .auth import _b64, _unb64

logger = logging.getLogger("grantwatch.web")

# The cost of one hash. Module constants so tests can make them cheap.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
HASH_BYTES = 32
# Browsers send Basic credentials as ASCII, so the command line asks for ASCII.
MIN_PASSWORD_LENGTH = 16

NAME = re.compile(r"[a-z0-9._-]{1,32}")
_SHOWN_NAME = re.compile(r"[A-Za-z0-9._-]{1,32}")
_NUMBER = re.compile(r"[0-9]{1,9}")
_BASE64 = re.compile(r"[A-Za-z0-9_-]+")
# What an unknown name is checked against, so it costs as much as a wrong password.
_DUMMY = (bytes(SALT_BYTES), bytes(HASH_BYTES))

_Hash = Tuple[int, int, int, bytes, bytes]


def canonical_name(name: str) -> Optional[str]:
    """The lowercase form of a valid account name, or None. Names are not case-sensitive."""
    lowered = name.lower() if name.isascii() else ""
    return lowered if NAME.fullmatch(lowered) else None


def _derive(password: str, n: int, r: int, p: int, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=HASH_BYTES)


def hash_password(password: str) -> str:
    """``scrypt$n$r$p$salt$hash`` for a password, with a new random salt."""
    salt = os.urandom(SALT_BYTES)
    digest = _derive(password, SCRYPT_N, SCRYPT_R, SCRYPT_P, salt)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def _parse_hash(text: str) -> Optional[_Hash]:
    parts = text.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return None
    if not all(_NUMBER.fullmatch(part) for part in parts[1:4]) or not all(_BASE64.fullmatch(part) for part in parts[4:]):
        return None
    n, r, p = (int(part) for part in parts[1:4])
    if n < 2 or n & (n - 1) or r < 1 or p < 1:  # scrypt wants a power of two for n
        return None
    try:
        salt, digest = _unb64(parts[4]), _unb64(parts[5])
    except ValueError:
        return None
    if len(salt) != SALT_BYTES or len(digest) != HASH_BYTES:
        return None
    return n, r, p, salt, digest


def parse_users(raw: Optional[str]) -> Dict[str, str]:
    """Account name to ``scrypt$...`` hash, from the GRANTS_ADMIN_USERS text. Never raises.

    A malformed entry is skipped with a warning that names the account at most,
    never the rest of the entry: it could be a hash, or a password pasted by mistake.
    """
    users: Dict[str, str] = {}
    entries = [entry.strip() for entry in re.split(r"[,\n]", raw or "")]
    for position, entry in enumerate((entry for entry in entries if entry), start=1):
        name, colon, spec = entry.partition(":")
        key = canonical_name(name)
        shown = f"'{name}'" if colon and _SHOWN_NAME.fullmatch(name) else f"number {position}"
        if key is None or _parse_hash(spec) is None:
            logger.warning("Skipping admin account %s in GRANTS_ADMIN_USERS: expected name:scrypt$n$r$p$salt$hash.", shown)
        elif key in users:
            logger.warning("Skipping admin account %s in GRANTS_ADMIN_USERS: the name is already used.", shown)
        else:
            users[key] = spec
    return users


def check(users: Dict[str, str], name: str, password: str) -> bool:
    """Whether the name and password belong to an account. Always costs one hash, known name or not."""
    key = canonical_name(name)
    stored = _parse_hash(users[key]) if key in users else None
    n, r, p, salt, digest = stored or (SCRYPT_N, SCRYPT_R, SCRYPT_P, *_DUMMY)
    try:
        derived = _derive(password, n, r, p, salt)
    except (ValueError, OverflowError):  # text that is not UTF-8, or a cost scrypt refuses
        return False
    return hmac.compare_digest(derived, digest) and stored is not None
