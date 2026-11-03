"""Tiny ULID generator (https://github.com/ulid/spec), monotonic per process."""

from __future__ import annotations

import os
import threading
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_lock = threading.Lock()
_last_ms = -1
_last_rand = 0


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def new_ulid(now_ms: int | None = None) -> str:
    """Return a 26-char Crockford base32 ULID; lexicographically sortable."""
    global _last_ms, _last_rand
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    with _lock:
        if ms <= _last_ms:
            # Same (or earlier) millisecond: increment randomness -> monotonic.
            ms = _last_ms
            rand = (_last_rand + 1) & ((1 << 80) - 1)
        else:
            rand = int.from_bytes(os.urandom(10), "big")
        _last_ms, _last_rand = ms, rand
    return _encode(ms & ((1 << 48) - 1), 10) + _encode(rand, 16)


def is_ulid(value: str) -> bool:
    return len(value) == 26 and all(c in _ALPHABET for c in value.upper())
