"""登录、会话与口令（第 7 层 api）。

零件（纯函数，不碰账本）：
- 口令哈希用标准库 ``hashlib.scrypt``，带随机盐，参数写进哈希串：``scrypt$N$r$p$盐(十六进制)$摘要(十六进制)``。
  校验时参数取自存下来的串（以后调高默认参数，旧哈希照常能验）；口令先做 NFKC 规整（全角半角等价）再按 UTF-8 编码。
  畸形或带恶意参数（会耗尽内存的 N）的哈希串一律判不匹配，不抛异常。
- 会话令牌是 32 字节随机数的 urlsafe 编码；账本里只存它的 SHA-256 十六进制。
- 本机令牌（命令行用）：``write_local_token`` 原子地写成 0600 的文件，返回令牌；每次内核启动重写一次。
- ``LoginLimiter``：按用户名的失败限速。连续失败达到 ``max_failures`` 次就锁 ``lock_s`` 秒；锁期内不再校验口令（口令对也不放行），
  询问不会延长锁；锁过期后再失败一次立刻重新锁（计数在 ``window_s`` 内不清零）；成功清零。内存有上限，锁着的用户名最后才被挤出。

服务（``AuthService``，经账本读写）：建操作员、登录、认证（本机令牌或会话令牌）、闲置过期、退出、重新校验口令。
- 登录与重新校验共用同一个按用户名的限速；同一用户名的校验串行（条纹锁），所以并发的错误猜测也越不过锁。
- 用户名不存在、操作员已停用、口令错误三者对外一模一样（401 ``bad_credentials``、同样的文字），不存在的用户名也要付一次口令校验的代价。
- 口令校验在线程里做（scrypt 约 0.3 秒，不堵事件循环）。令牌、口令永不进日志与审计。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import os
import re
import secrets
import tempfile
import unicodedata
import zlib
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dmr_kernel.api import errors
from dmr_kernel.api.common import LoginResult, Principal, SeatChanged
from dmr_kernel.core.clock import Clock
from dmr_kernel.core.events import EventBus
from dmr_kernel.ledger import repo_audit, repo_auth
from dmr_kernel.ledger.records import OperatorRecord
from dmr_kernel.ledger.writer import Ledger

__all__ = [
    "ACTIVITY_WRITE_EVERY_MS", "AuthService", "DEFAULT_SCRYPT", "DIGEST_BYTES", "LoginLimiter", "PASSWORD_MAX_LEN",
    "PASSWORD_MIN_LEN", "ROLE_RE", "SALT_BYTES", "ScryptParams", "USERNAME_RE", "bearer_token", "hash_password",
    "hash_token", "new_token", "verify_password", "write_local_token",
]

SALT_BYTES = 16
DIGEST_BYTES = 32
PASSWORD_MIN_LEN = 8  # 新口令的最短长度（建操作员时检查；校验时不限制，免得旧口令进不来）
PASSWORD_MAX_LEN = 1024
MAX_ENCODED_BYTES = 8192  # NFKC 之后的字节数上限（个别字符会展开成多个）
TOKEN_BYTES = 32
LOCAL_TOKEN_MODE = 0o600
LOCAL_TOKEN_DIR_MODE = 0o700
LOCAL_TOKEN_MIN_LEN = 32  # 本机令牌太短（或为空）等于没有鉴权：构造 AuthService 时拒绝
MAX_TOKEN_CHARS = 512  # 请求里带来的令牌超过这个长度直接判无效，不去哈希、不去查库
ACTIVITY_WRITE_EVERY_MS = 30_000  # 会话活动时间最多这么久往账本写一次（内存里的活动时间每次都算）
STRIPES = 32  # 同一用户名的校验串行：按用户名散列到这么多把锁
MAX_CONCURRENT_HASHES = 4  # 同时在线程里算 scrypt 的上限（每个约 32 MiB 内存），其余的排队
USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,32}")
ROLE_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")

# 参数的上下界：下界防止配错成弱参数，上界防止存进库里的畸形哈希串耗尽内存（128 * r * N 字节：上界约 256 MiB）
MIN_N, MAX_N = 2**4, 2**17
MAX_R, MAX_P = 16, 16
MIN_SALT, MAX_SALT = 8, 64
MIN_DIGEST, MAX_DIGEST = 16, 64


@dataclass(frozen=True)
class ScryptParams:
    # OWASP 的 scrypt 等价组合之一（N=2^15 约 32 MiB 内存，p=3）
    n: int = 2**15
    r: int = 8
    p: int = 3
    salt_len: int = SALT_BYTES
    dklen: int = DIGEST_BYTES


DEFAULT_SCRYPT = ScryptParams()


def _check(n: int, r: int, p: int, salt_len: int, dklen: int) -> None:
    if n < MIN_N or n > MAX_N or n & (n - 1):
        raise ValueError(f"scrypt 的 N 必须是 {MIN_N} 到 {MAX_N} 之间的 2 的幂：{n}")
    if not 1 <= r <= MAX_R or not 1 <= p <= MAX_P:
        raise ValueError(f"scrypt 的 r 必须在 1 到 {MAX_R}、p 必须在 1 到 {MAX_P} 之间：r={r} p={p}")
    if not MIN_SALT <= salt_len <= MAX_SALT or not MIN_DIGEST <= dklen <= MAX_DIGEST:
        raise ValueError(f"盐长度必须在 {MIN_SALT} 到 {MAX_SALT}、摘要长度在 {MIN_DIGEST} 到 {MAX_DIGEST} 字节之间")


def _prepare(password: str) -> bytes:
    if not isinstance(password, str) or not 1 <= len(password) <= PASSWORD_MAX_LEN:
        raise ValueError(f"口令长度必须在 1 到 {PASSWORD_MAX_LEN} 个字符之间")
    data = unicodedata.normalize("NFKC", password).encode("utf-8")
    if len(data) > MAX_ENCODED_BYTES:
        raise ValueError("口令太长")
    return data


def _scrypt(data: bytes, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    # OpenSSL 默认只给 32 MiB；显式给够 128 * r * (N + p + 2) 再加 1 MiB 余量
    return hashlib.scrypt(data, salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=128 * r * (n + p + 2) + 2**20)


def hash_password(password: str, params: ScryptParams = DEFAULT_SCRYPT) -> str:
    _check(params.n, params.r, params.p, params.salt_len, params.dklen)
    data = _prepare(password)
    salt = secrets.token_bytes(params.salt_len)
    digest = _scrypt(data, salt, params.n, params.r, params.p, params.dklen)
    return f"scrypt${params.n}${params.r}${params.p}${salt.hex()}${digest.hex()}"


def _parse(stored: str) -> tuple[int, int, int, bytes, bytes] | None:
    if not isinstance(stored, str):
        return None
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return None
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt, expected = bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
        _check(n, r, p, len(salt), len(expected))
    except ValueError:
        return None
    return n, r, p, salt, expected


def verify_password(password: str, stored: str) -> bool:
    """口令是否与存下来的哈希串匹配（常量时间比较）。哈希串畸形、口令超长或为空都返回 False。"""
    parsed = _parse(stored)
    if parsed is None:
        return False
    n, r, p, salt, expected = parsed
    try:
        data = _prepare(password)
        actual = _scrypt(data, salt, n, r, p, len(expected))
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------- 令牌


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bearer_token(header: str | None) -> str | None:
    """从 ``Authorization`` 头取 Bearer 令牌；缺失或格式不对返回 None。"""
    if not header:
        return None
    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


def write_local_token(path: Path) -> str:
    """生成本机令牌，原子地写进 ``path``（临时文件 + rename，0600；父目录不存在就建成 0700），返回令牌。
    已有的文件（哪怕是符号链接）被替换，不跟随链接写到别处。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=LOCAL_TOKEN_DIR_MODE)
    token = new_token()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), LOCAL_TOKEN_MODE)
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
    return token


# ---------------------------------------------------------------- 失败限速


@dataclass
class _Entry:
    failures: int
    last_ms: int
    locked_until_ms: int = 0


class LoginLimiter:
    def __init__(self, clock: Clock, *, max_failures: int = 5, lock_s: int = 60, window_s: int = 900,
                 max_entries: int = 1024) -> None:
        if min(max_failures, lock_s, window_s, max_entries) < 1:
            raise ValueError("max_failures、lock_s、window_s、max_entries 都必须是正整数")
        self._clock = clock
        self._max_failures = max_failures
        self._lock_ms = lock_s * 1000
        self._window_ms = window_s * 1000
        self._max_entries = max_entries
        self._entries: dict[str, _Entry] = {}

    @staticmethod
    def _key(username: str) -> str:
        return username.strip()

    def size(self) -> int:
        return len(self._entries)

    def retry_after_ms(self, username: str) -> int:
        """还要等多久才能再试；没被锁返回 0。询问不改变任何状态。"""
        entry = self._entries.get(self._key(username))
        if entry is None:
            return 0
        return max(entry.locked_until_ms - self._clock.now_ms(), 0)

    def record_failure(self, username: str) -> int:
        """记一次失败；返回这次失败之后还要等多久（刚好锁上时是整个锁期，否则 0）。已锁着的不延长。"""
        now = self._clock.now_ms()
        key = self._key(username)
        entry = self._entries.get(key)
        if entry is not None and entry.locked_until_ms > now:
            return entry.locked_until_ms - now
        if entry is None or now - entry.last_ms > self._window_ms:
            entry = self._entries[key] = _Entry(failures=0, last_ms=now)
        entry.failures += 1
        entry.last_ms = now
        if entry.failures >= self._max_failures:
            entry.locked_until_ms = now + self._lock_ms
        self._evict(now)
        return max(entry.locked_until_ms - now, 0)

    def record_success(self, username: str) -> None:
        self._entries.pop(self._key(username), None)

    def _evict(self, now: int) -> None:
        if len(self._entries) <= self._max_entries:
            return
        for key in [k for k, e in self._entries.items()
                    if e.locked_until_ms <= now and now - e.last_ms > self._window_ms]:
            del self._entries[key]
        if len(self._entries) <= self._max_entries:
            return
        unlocked = sorted((k for k, e in self._entries.items() if e.locked_until_ms <= now),
                          key=lambda k: self._entries[k].last_ms)
        for key in unlocked:
            if len(self._entries) <= self._max_entries:
                return
            del self._entries[key]
        for key in sorted(self._entries, key=lambda k: self._entries[k].locked_until_ms):
            if len(self._entries) <= self._max_entries:
                return
            del self._entries[key]


# ---------------------------------------------------------------- 服务


class AuthService:
    """登录与会话。``config_get`` 取 ``session_idle_min``、``reauth_window_s``（每次都读，改配置马上生效）。"""

    def __init__(self, ledger: Ledger, clock: Clock, bus: EventBus, *, config_get: Callable[[str], Any],
                 local_token: str, params: ScryptParams = DEFAULT_SCRYPT, limiter: LoginLimiter | None = None) -> None:
        if not isinstance(local_token, str) or len(local_token) < LOCAL_TOKEN_MIN_LEN:
            raise ValueError(f"本机令牌至少 {LOCAL_TOKEN_MIN_LEN} 个字符：空的或太短的令牌等于没有鉴权")
        self._ledger = ledger
        self._clock = clock
        self._bus = bus
        self._config_get = config_get
        self._local_token = local_token.encode("utf-8")
        self._params = params
        self._limiter = limiter if limiter is not None else LoginLimiter(clock)
        # 不存在的用户名也校验一次，耗时与真用户相近；哈希是随机口令的，永远对不上
        self._dummy_hash = hash_password(secrets.token_urlsafe(16), params)
        self._stripes = [asyncio.Lock() for _ in range(STRIPES)]
        self._hash_slots = asyncio.Semaphore(MAX_CONCURRENT_HASHES)
        self._seen: dict[int, int] = {}  # 会话号 -> 内存里记的最近活动时间
        self._written: dict[int, int] = {}  # 会话号 -> 最近一次写进账本的活动时间

    # ------------------------------------------------------------ 配置与小工具

    def _config_int(self, key: str) -> int:
        return int(self._config_get(key))

    def _stripe(self, username: str) -> asyncio.Lock:
        return self._stripes[zlib.crc32(username.strip().encode("utf-8")) % STRIPES]

    def _forget(self, session_id: int) -> None:
        self._seen.pop(session_id, None)
        self._written.pop(session_id, None)

    async def _verify(self, password: str, stored: str) -> bool:
        async with self._hash_slots:  # 并发的口令校验排队：内存和 CPU 都有上限
            return await asyncio.to_thread(verify_password, password, stored)

    def _check_not_locked(self, username: str) -> None:
        wait_ms = self._limiter.retry_after_ms(username)
        if wait_ms > 0:
            raise errors.rate_limited(math.ceil(wait_ms / 1000))

    async def _register_failure(self, username: str, operator: OperatorRecord | None, action: str) -> None:
        """记一次失败：限速计数加一；已存在的操作员另写审计（不存在的用户名不写，免得被刷满审计表）。"""
        retry_ms = self._limiter.record_failure(username)
        if operator is None:
            return
        now = self._clock.now_ms()
        target = f"operator:{operator.username}"
        await self._ledger.write(repo_audit.append, at_ms=now, operator=operator.username, action=action,
                                 target=target, result="bad_credentials")
        if retry_ms > 0:
            await self._ledger.write(repo_audit.append, at_ms=now, operator=operator.username,
                                     action="auth.login_locked", target=target,
                                     after={"retry_after_s": math.ceil(retry_ms / 1000)}, result="rate_limited")

    # ------------------------------------------------------------ 操作员

    async def create_operator(self, username: str, password: str, *, role: str = "admin",
                              created_by: str = repo_auth.SYSTEM) -> OperatorRecord:
        """建操作员（口令存 scrypt 哈希）。用户名 1–32 位字母数字或 ``_ . -``，区分大小写；口令 8–1024 个字符。
        用户名已存在抛 ``repo_auth.OperatorExists``；其余不合规抛 ``ValueError``。"""
        if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
            raise ValueError("用户名只能是 1 到 32 位的字母、数字、下划线、点或连字符")
        if not isinstance(role, str) or not ROLE_RE.fullmatch(role):
            raise ValueError("角色只能是小写字母开头、最长 32 位的小写字母、数字或下划线")
        if not isinstance(password, str) or not PASSWORD_MIN_LEN <= len(password) <= PASSWORD_MAX_LEN:
            raise ValueError(f"口令长度必须在 {PASSWORD_MIN_LEN} 到 {PASSWORD_MAX_LEN} 个字符之间")
        async with self._hash_slots:
            password_hash = await asyncio.to_thread(hash_password, password, self._params)
        return await self._ledger.write(repo_auth.create_operator, username=username, password_hash=password_hash,
                                        role=role, now_ms=self._clock.now_ms(), created_by=created_by)

    # ------------------------------------------------------------ 登录与退出

    async def login(self, username: str, password: str, *, client_info: str = "") -> LoginResult:
        async with self._stripe(username):  # 同一用户名的校验一个接一个：并发的错误猜测越不过锁
            self._check_not_locked(username)
            operator = await self._ledger.read(repo_auth.get_operator_by_name, username)
            usable = operator is not None and not operator.disabled
            stored = operator.password_hash if usable else self._dummy_hash
            matched = await self._verify(password, stored)
            if not (usable and matched):
                await self._register_failure(username, operator, "auth.login_failed")
                raise errors.bad_credentials()
            self._limiter.record_success(username)
        now = self._clock.now_ms()
        token = new_token()
        session = await self._ledger.write(repo_auth.create_session, token_hash=hash_token(token),
                                           operator_id=operator.id, now_ms=now, client_info=client_info)
        self._prune_activity(now)
        return LoginResult(token=token, operator=operator.username, session_id=session.id,
                           idle_timeout_min=self._config_int("session_idle_min"))

    async def logout(self, principal: Principal) -> bool:
        """撤销会话；它持有席位就一并放掉并发布 ``SeatChanged``。返回有没有放掉席位。重复退出无害（返回 False）。"""
        if not principal.is_session:
            raise errors.not_allowed("本机令牌没有会话，无需退出")
        now = self._clock.now_ms()
        result = await self._ledger.write(repo_auth.end_session, principal.session_id, now_ms=now, reason="logout")
        self._forget(principal.session_id)
        if result.seat_released:
            self._bus.publish(SeatChanged(holder_session_id=None, cause="logout", at_ms=now))
        return result.seat_released

    # ------------------------------------------------------------ 认证

    async def authenticate(self, token: str | None, *, touch: bool = True) -> Principal:
        """令牌换主体。本机令牌 -> ``cli``（不碰账本）；会话令牌 -> ``session``。无效、已撤销、操作员已停用、
        闲置超过 ``session_idle_min`` 分钟（恰好等于还算有效）都是 401 ``unauthorized``；闲置过期的会话当场撤销。
        ``touch=True`` 记下这次活动（内存每次记，账本最多每 30 秒写一次）。"""
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS:
            raise errors.unauthorized()
        if hmac.compare_digest(token.encode("utf-8"), self._local_token):
            return Principal.cli()
        found = await self._ledger.read(repo_auth.get_session_context, hash_token(token))
        if found is None or found.session.revoked or found.operator.disabled:
            raise errors.unauthorized()
        session, operator = found.session, found.operator
        now = self._clock.now_ms()
        idle_min = self._config_int("session_idle_min")
        last_active = max(session.last_active_ms, self._seen.get(session.id, 0))
        if now - last_active > idle_min * 60_000:
            await self._expire(session.id, now)
            raise errors.unauthorized(f"会话已过期：闲置超过 {idle_min} 分钟，请重新登录")
        if touch:
            await self._touch(session.id, session.last_active_ms, now)
        return Principal(kind="session", operator=operator.username, session_id=session.id, operator_id=operator.id,
                         role=operator.role, reauth_ms=session.reauth_ms)

    async def _expire(self, session_id: int, now: int) -> None:
        result = await self._ledger.write(repo_auth.end_session, session_id, now_ms=now, reason="idle")
        self._forget(session_id)
        if result.seat_released:
            self._bus.publish(SeatChanged(holder_session_id=None, cause="expired", at_ms=now))

    async def _touch(self, session_id: int, stored_ms: int, now: int) -> None:
        self._seen[session_id] = max(self._seen.get(session_id, 0), now)
        written = max(stored_ms, self._written.get(session_id, 0))
        if now - written >= ACTIVITY_WRITE_EVERY_MS:
            self._written[session_id] = now
            await self._ledger.write(repo_auth.touch_session, session_id, now_ms=now)

    def _prune_activity(self, now: int) -> None:
        """登录时顺手清掉早已闲置过期、再没人来认证的会话在内存里留下的记录。"""
        horizon = now - self._config_int("session_idle_min") * 60_000
        for session_id in [sid for sid, seen in self._seen.items() if seen < horizon]:
            self._forget(session_id)

    # ------------------------------------------------------------ 重新校验口令

    async def reauth(self, principal: Principal, password: str, *, via: str = "reauth") -> int:
        """当场校验口令并记下这次校验的时间；返回有效期的结束时刻。失败与登录共用同一个限速（按用户名）。
        ``via``：写进审计的来源（reauth，或强制接管席位时的 seat_force）。"""
        if not principal.is_session:
            raise errors.not_allowed("本机令牌没有口令，无需重新校验")
        async with self._stripe(principal.operator):
            self._check_not_locked(principal.operator)
            operator = await self._ledger.read(repo_auth.get_operator, principal.operator_id)
            if operator is None or operator.disabled:
                raise errors.unauthorized()
            matched = await self._verify(password, operator.password_hash)
            if not matched:
                await self._register_failure(principal.operator, operator, "auth.reauth_failed")
                raise errors.bad_credentials("口令错误")
            self._limiter.record_success(principal.operator)
        now = self._clock.now_ms()
        try:
            await self._ledger.write(repo_auth.set_reauth, principal.session_id, now_ms=now, via=via)
        except repo_auth.SessionInactive:
            raise errors.unauthorized("会话已失效，请重新登录") from None
        return now + self._config_int("reauth_window_s") * 1000

    def reauth_valid_until(self, principal: Principal) -> int | None:
        """最近一次口令校验的有效期结束时刻；没校验过、已过期（恰好到点就算过期）或本机令牌都是 None。"""
        if not principal.is_session or principal.reauth_ms is None:
            return None
        until = principal.reauth_ms + self._config_int("reauth_window_s") * 1000
        return until if self._clock.now_ms() < until else None

    def require_recent_reauth(self, principal: Principal) -> None:
        """要求窗口内校验过口令（高风险操作用）；本机令牌不需要。"""
        if principal.is_session and self.reauth_valid_until(principal) is None:
            raise errors.reauth_required()
