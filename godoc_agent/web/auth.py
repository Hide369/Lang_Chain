"""管理者ログイン。パスワードはハッシュだけを保存し、セッションはサーバーのメモリに持つ。

管理者は1種類だけ（パスワードを知っている人）。サーバーを再起動するとログイン状態は消える。
"""

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field

# scrypt のパラメータ（OWASP 推奨の最小値 N=2^17 は1回 128MB 使うため、ここでは 2^15 = 32MB に抑える）
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_PREFIX = "scrypt"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def hash_password(password: str, *, n: int = _SCRYPT_N) -> str:
    """`scrypt:N:r:p:salt:hash` 形式。.env に書けるよう `$` を含めない。"""
    if not password:
        raise ValueError("パスワードが空です")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=_SCRYPT_R, p=_SCRYPT_P, maxmem=_SCRYPT_MAXMEM)
    return ":".join([_PREFIX, str(n), str(_SCRYPT_R), str(_SCRYPT_P), _b64(salt), _b64(digest)])


def validate_password_hash(stored: str) -> None:
    """起動時に形式を確認する（壊れた値で起動し、誰もログインできない状態を避ける）。"""
    parts = stored.split(":")
    if len(parts) != 6 or parts[0] != _PREFIX:
        raise ValueError("環境変数 WEB_ADMIN_PASSWORD_HASH の形式が正しくありません。godoc_agent.web.password で作り直してください")
    try:
        int(parts[1]), int(parts[2]), int(parts[3])
        base64.urlsafe_b64decode(parts[4]), base64.urlsafe_b64decode(parts[5])
    except ValueError as e:
        raise ValueError("環境変数 WEB_ADMIN_PASSWORD_HASH の形式が正しくありません。godoc_agent.web.password で作り直してください") from e


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, expected = stored.split(":")
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=base64.urlsafe_b64decode(salt),
            n=int(n), r=int(r), p=int(p), maxmem=_SCRYPT_MAXMEM,
        )
    except ValueError:
        return False
    return hmac.compare_digest(digest, base64.urlsafe_b64decode(expected))


class SessionStore:
    """ログイン中のセッション（トークン → 有効期限）。"""

    def __init__(self, ttl_s: int, clock=time.monotonic):
        self._ttl_s = ttl_s
        self._clock = clock
        self._sessions: dict[str, float] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge()
            self._sessions[token] = self._clock() + self._ttl_s
        return token

    def is_valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            expires = self._sessions.get(token)
            if expires is None:
                return False
            if expires <= self._clock():
                del self._sessions[token]
                return False
            return True

    def revoke(self, token: str | None) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    def _purge(self) -> None:
        now = self._clock()
        for token in [t for t, exp in self._sessions.items() if exp <= now]:
            del self._sessions[token]


@dataclass
class _Failures:
    count: int = 0
    first_at: float = 0.0
    locked_until: float = 0.0


@dataclass
class LoginThrottle:
    """パスワードの総当たり対策。接続元ごとに、一定時間内の失敗が続いたらしばらく受け付けない。"""

    max_failures: int = 5
    window_s: float = 15 * 60
    lockout_s: float = 15 * 60
    clock: object = time.monotonic
    _failures: dict[str, _Failures] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def retry_after(self, client: str) -> float:
        """受け付けない残り秒数（0 なら受け付ける）。"""
        with self._lock:
            entry = self._failures.get(client)
            return max(0.0, entry.locked_until - self.clock()) if entry else 0.0

    def record_failure(self, client: str) -> None:
        now = self.clock()
        with self._lock:
            entry = self._failures.setdefault(client, _Failures(first_at=now))
            if now - entry.first_at > self.window_s:
                entry.count, entry.first_at = 0, now
            entry.count += 1
            if entry.count >= self.max_failures:
                entry.locked_until = now + self.lockout_s
                entry.count, entry.first_at = 0, now

    def record_success(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)
