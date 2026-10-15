"""
认证核心模块

提供密码生成、JWT token 创建/验证、凭据校验等功能。
同时支持 API Key 认证（`arc-` hoặc 的 Bearer token）。
"""

import hashlib
import json
import logging
import os
import secrets
import string
import time
from collections import OrderedDict
from datetime import UTC
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import jwt
from fastapi import Depends, HTTPException, Query
from fastapi.security import OAuth2PasswordBearer
from jwt import PyJWKClient
from pwdlib import PasswordHash
from pydantic import BaseModel, ConfigDict

from lib import PROJECT_ROOT

logger = logging.getLogger(__name__)


class CurrentUserInfo(BaseModel):
    """Current authenticated user info."""

    id: str
    sub: str
    role: str = "viewer"
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    source: str = "legacy"

    model_config = ConfigDict(frozen=True)

    @property
    def is_admin(self) -> bool:
        return self.role.strip().lower() in {"admin", "administrator", "owner"}

    @property
    def is_operator(self) -> bool:
        return _ROLE_ORDER.get(self.role.strip().lower(), -1) >= _ROLE_ORDER["operator"]


# JWT 签名密钥缓存
_cached_token_secret: str | None = None
_logto_jwks_client: tuple[str, PyJWKClient] | None = None

_ROLE_ORDER = {
    "viewer": 0,
    "operator": 1,
    "manager": 2,
    "owner": 3,
    "administrator": 4,
    "admin": 4,
}
_LOGTO_ROLE_ALIASES = {
    "admin": "administrator",
    "administrator": "administrator",
    "owner": "owner",
    "manager": "manager",
    "operator": "operator",
    "viewer": "viewer",
    "user": "viewer",
}

# Token 有效期：7 天
TOKEN_EXPIRY_SECONDS = 7 * 24 * 3600

# 关闭认证时返回的匿名用户标识
_ANONYMOUS_USER_SUB = "local"

# 视为"关闭认证"的 env 取值。空串不在内 —— .env 误写 `AUTH_ENABLED=` 应回退到默认（开启），
# 避免静默 fail-open。
_AUTH_DISABLED_VALUES = frozenset({"false", "0", "no", "off"})


def is_auth_enabled() -> bool:
    """``AUTH_ENABLED`` env 解析。默认 ``true``，保持现有部署行为；空值也按默认。

    ``false`` / ``0`` / ``no`` / ``off`` 一律视为关闭（不区分大小写）。
    """
    return os.environ.get("AUTH_ENABLED", "true").strip().lower() not in _AUTH_DISABLED_VALUES


def logto_issuer() -> str:
    """Return the project-scoped Logto issuer, without cross-project fallback."""
    return os.environ.get("STUDIO360_LOGTO_ISSUER", "").strip().rstrip("/")


def logto_resource() -> str:
    """Return the project-scoped Logto API resource/audience."""
    return os.environ.get("STUDIO360_LOGTO_API_RESOURCE", "").strip().rstrip("/")


def logto_jwks_uri() -> str:
    configured = os.environ.get("STUDIO360_LOGTO_JWKS_URI", "").strip()
    return configured or f"{logto_issuer()}/jwks"


def _is_https_or_local_http(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme == "https" or (
        parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    )


def logto_configuration_error() -> str | None:
    """Validate the minimum project-scoped Logto resource contract."""
    issuer = logto_issuer()
    resource = logto_resource()
    jwks = os.environ.get("STUDIO360_LOGTO_JWKS_URI", "").strip()
    if not issuer and not resource and not jwks:
        return None
    if not issuer or not resource:
        return "STUDIO360_LOGTO_ISSUER and STUDIO360_LOGTO_API_RESOURCE must be configured together."
    if not _is_https_or_local_http(issuer):
        return "STUDIO360_LOGTO_ISSUER must use HTTPS outside local development."
    if jwks and not _is_https_or_local_http(jwks):
        return "STUDIO360_LOGTO_JWKS_URI must use HTTPS outside local development."
    if not _is_https_or_local_http(resource):
        return "STUDIO360_LOGTO_API_RESOURCE must use HTTPS outside local development."
    return None


def is_logto_configured() -> bool:
    """Whether this process opted into the Studio360 Logto bearer contract."""
    return bool(logto_issuer() or logto_resource() or os.environ.get("STUDIO360_LOGTO_JWKS_URI", "").strip())


def _get_logto_signing_key(token: str) -> Any:
    """Resolve a signing key from the project-scoped JWKS endpoint."""
    global _logto_jwks_client
    uri = logto_jwks_uri()
    if _logto_jwks_client is None or _logto_jwks_client[0] != uri:
        _logto_jwks_client = (uri, PyJWKClient(uri, cache_keys=True, lifespan=300))
    return _logto_jwks_client[1].get_signing_key_from_jwt(token).key


def verify_logto_token(token: str) -> dict[str, Any]:
    """Validate a Logto access token with pinned issuer, audience and algorithm."""
    error = logto_configuration_error()
    if error:
        raise HTTPException(status_code=503, detail="Logto authentication is misconfigured.")
    if not is_logto_configured():
        raise HTTPException(status_code=503, detail="Logto authentication is not configured.")

    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256":
            raise jwt.InvalidAlgorithmError("unsupported JWT algorithm")
        payload = jwt.decode(
            token,
            _get_logto_signing_key(token),
            algorithms=["RS256"],
            issuer=logto_issuer(),
            audience=logto_resource(),
            options={"require": ["sub", "iss", "aud", "iat", "exp"]},
        )
    except HTTPException:
        raise
    except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
        logger.info("Logto access token rejected: %s", type(exc).__name__)
        raise HTTPException(
            status_code=401,
            detail="Logto access token is invalid or expired.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    if not isinstance(payload, dict) or not str(payload.get("sub") or "").strip():
        raise HTTPException(
            status_code=401,
            detail="Logto access token is missing a subject.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


def _anonymous_user() -> "CurrentUserInfo":
    """关闭认证时返回的固定匿名用户。"""
    from lib.db.base import DEFAULT_USER_ID

    # This is an explicit local development kill-switch, not a default for
    # authenticated identities. Keep its elevated role so an operator can
    # still exercise the full local UI when AUTH_ENABLED=false.
    return CurrentUserInfo(
        id=DEFAULT_USER_ID,
        sub=_ANONYMOUS_USER_SUB,
        role="admin",
        roles=("admin",),
        source="local-bypass",
    )


# OAuth2 scheme
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")
oauth2_scheme_optional = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token", auto_error=False)

# 密码哈希
_password_hash = PasswordHash.recommended()
_cached_password_hash: str | None = None


def generate_password(length: int = 16) -> str:
    """生成随机字母数字密码"""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def get_token_secret() -> str:
    """获取 JWT 签名密钥

    优先使用 AUTH_TOKEN_SECRET 环境变量，否则自动生成并缓存。
    """
    global _cached_token_secret

    env_secret = os.environ.get("AUTH_TOKEN_SECRET")
    if env_secret:
        return env_secret

    if _cached_token_secret is not None:
        return _cached_token_secret

    _cached_token_secret = secrets.token_hex(32)
    logger.info("已自动生成 JWT 签名密钥")
    return _cached_token_secret


def create_token(username: str) -> str:
    """创建 JWT token

    Args:
        username: 用户名

    Returns:
        JWT token 字符串
    """
    now = time.time()
    payload = {
        "sub": username,
        # Legacy password login is a single local operator account. Explicitly
        # carry its role so the role mapper never needs an implicit admin
        # default for arbitrary signed tokens.
        "role": "admin",
        "roles": ["admin"],
        "iat": now,
        "exp": now + TOKEN_EXPIRY_SECONDS,
    }
    return jwt.encode(payload, get_token_secret(), algorithm="HS256")


def verify_token(token: str) -> dict | None:
    """验证 JWT token

    Args:
        token: JWT token 字符串

    Returns:
        成功返回 payload dict，失败返回 None
    """
    try:
        payload = jwt.decode(token, get_token_secret(), algorithms=["HS256"])
        return payload
    except (jwt.InvalidTokenError, jwt.ExpiredSignatureError):
        return None


DOWNLOAD_TOKEN_EXPIRY_SECONDS = 300  # 5 分钟


def create_download_token(username: str, project_name: str) -> str:
    """签发短时效下载 token，用于浏览器原生下载认证"""
    now = time.time()
    payload = {
        "sub": username,
        "project": project_name,
        "purpose": "download",
        "iat": now,
        "exp": now + DOWNLOAD_TOKEN_EXPIRY_SECONDS,
    }
    return jwt.encode(payload, get_token_secret(), algorithm="HS256")


def verify_download_token(token: str, project_name: str) -> dict:
    """验证下载 token

    Returns:
        成功返回 payload dict

    Raises:
        jwt.ExpiredSignatureError: token 已过期
        jwt.InvalidTokenError: token 无效
        ValueError: purpose 或 project 不匹配
    """
    if not is_auth_enabled():
        return {
            "sub": _ANONYMOUS_USER_SUB,
            "project": project_name,
            "purpose": "download",
        }
    payload = jwt.decode(token, get_token_secret(), algorithms=["HS256"])
    if payload.get("purpose") != "download":
        raise ValueError("Mục đích token không khớp (token purpose mismatch)")
    if payload.get("project") != project_name:
        raise ValueError("Dự án token không khớp (token project mismatch)")
    return payload


def _get_password_hash() -> str:
    """获取当前密码的哈希值（缓存）"""
    global _cached_password_hash
    if _cached_password_hash is None:
        raw = os.environ.get("AUTH_PASSWORD", "")
        _cached_password_hash = _password_hash.hash(raw)
    return _cached_password_hash


def check_credentials(username: str, password: str) -> bool:
    """校验用户名密码（使用哈希比对）

    从 AUTH_USERNAME（默认 admin）和 AUTH_PASSWORD 环境变量读取。
    即使用户名不匹配也执行哈希验证，防止时序攻击。

    ``AUTH_ENABLED=false`` 时无条件返回 True。
    """
    if not is_auth_enabled():
        return True
    expected_username = os.environ.get("AUTH_USERNAME", "admin")
    pw_hash = _get_password_hash()
    username_ok = secrets.compare_digest(username, expected_username)
    password_ok = _password_hash.verify(password, pw_hash)
    return username_ok and password_ok


def ensure_auth_password(env_path: str | None = None) -> str:
    """确保 AUTH_PASSWORD 已设置

    如果 AUTH_PASSWORD 环境变量为空，自动生成密码，写入环境变量，
    回写到 .env 文件，并用 logger.warning 输出到控制台。

    ``AUTH_ENABLED=false`` 时整个步骤跳过（不生成、不回写）。

    Args:
        env_path: .env 文件路径，默认为项目根目录的 .env

    Returns:
        当前的 AUTH_PASSWORD 值；关闭认证时返回空串。
    """
    if not is_auth_enabled():
        return ""
    password = os.environ.get("AUTH_PASSWORD")
    if password:
        return password

    # 自动生成密码
    password = generate_password()
    os.environ["AUTH_PASSWORD"] = password

    # 回写到 .env 文件
    if env_path is None:
        env_path = str(PROJECT_ROOT / ".env")

    env_file = Path(env_path)
    try:
        if env_file.exists():
            try:
                lines = env_file.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                # 历史 .env 可能用 cp936 / ANSI 等本地编码（早期 Windows 用户写过中文注释/值）；
                # 不强制覆写以免丢失用户内容，仅 log 并跳过自动回写。
                # 进程内 password 已 set 到 os.environ，本次启动仍可用，只是不持久化。
                logger.warning(
                    "无法以 UTF-8 解码 %s，跳过 AUTH_PASSWORD 自动回写；"
                    "请将该文件转存为 UTF-8 后重启以持久化生成的密码",
                    env_path,
                )
                return password
            new_lines = []
            found = False
            for line in lines:
                if not found and line.strip().startswith("AUTH_PASSWORD="):
                    new_lines.append(f"AUTH_PASSWORD={password}")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append(f"AUTH_PASSWORD={password}")
            new_content = "\n".join(new_lines) + "\n"
            # 使用原地写入（truncate + write）保留 inode，兼容 Docker bind mount
            with open(env_file, "r+", encoding="utf-8") as f:
                f.seek(0)
                f.write(new_content)
                f.truncate()
        else:
            env_file.write_text(f"AUTH_PASSWORD={password}\n", encoding="utf-8")
    except OSError:
        logger.warning("无法写入 .env 文件: %s", env_path)

    logger.warning("已自动生成认证密码，请查看 .env 文件中的 AUTH_PASSWORD 字段")
    return password


# ---------------------------------------------------------------------------
# API Key 认证支持
# ---------------------------------------------------------------------------

API_KEY_PREFIX = "std-"
API_KEY_CACHE_TTL = 300  # 5 分钟

# LRU 缓存：key_hash → (payload_dict | None, expires_at_timestamp)
# payload 为 None 表示 key 不存在或已过期（负缓存）
# 使用 OrderedDict 实现 LRU：命中时 move_to_end，淘汰时 popitem(last=False)
_api_key_cache: OrderedDict[str, tuple[dict | None, float]] = OrderedDict()
_API_KEY_CACHE_MAX = 512


def _hash_api_key(key: str) -> str:
    """计算 API Key 的 SHA-256 哈希。"""
    return hashlib.sha256(key.encode()).hexdigest()


def invalidate_api_key_cache(key_hash: str) -> None:
    """立即清除指定 key_hash 的缓存条目（key 删除时调用）。"""
    _api_key_cache.pop(key_hash, None)


def _get_cached_api_key_payload(key_hash: str) -> tuple[bool, dict | None]:
    """从缓存中查找。返回 (命中, payload 或 None)。命中时将条目移至末尾（LRU）。"""
    entry = _api_key_cache.get(key_hash)
    if entry is None:
        return False, None
    payload, expiry = entry
    if time.monotonic() > expiry:
        _api_key_cache.pop(key_hash, None)
        return False, None
    _api_key_cache.move_to_end(key_hash)
    return True, payload


def _set_api_key_cache(key_hash: str, payload: dict | None, expires_at_ts: float | None = None) -> None:
    """写入缓存（含 LRU 淘汰）。

    正向缓存（payload 非 None）TTL 以 key 实际过期时间为上界，
    避免 key 过期后仍在缓存中通过验证的安全问题。
    """
    if len(_api_key_cache) >= _API_KEY_CACHE_MAX:
        # 淘汰最久未使用的条目（LRU：OrderedDict 头部）
        _api_key_cache.popitem(last=False)
    ttl = API_KEY_CACHE_TTL
    if payload is not None and expires_at_ts is not None:
        time_to_expiry = expires_at_ts - time.monotonic()
        if time_to_expiry <= 0:
            # key 已过期，写入负缓存
            _api_key_cache[key_hash] = (None, time.monotonic() + API_KEY_CACHE_TTL)
            return
        ttl = min(ttl, time_to_expiry)
    _api_key_cache[key_hash] = (payload, time.monotonic() + ttl)


async def _verify_api_key(token: str) -> dict | None:
    """验证 API Key token，返回 payload dict 或 None（失败/过期/不存在）。

    内部先查缓存，缓存未命中再查数据库。
    查库成功后更新 last_used_at（后台异步，不阻塞响应）。
    """
    key_hash = _hash_api_key(token)

    # 缓存查询
    hit, cached_payload = _get_cached_api_key_payload(key_hash)
    if hit:
        return cached_payload

    # 数据库查询
    from lib.db import async_session_factory
    from lib.db.repositories.api_key_repository import ApiKeyRepository

    async with async_session_factory() as session:
        async with session.begin():
            repo = ApiKeyRepository(session)
            row = await repo.get_by_hash(key_hash)

    if row is None:
        _set_api_key_cache(key_hash, None)
        return None

    # 检查过期
    expires_at = row.get("expires_at")
    expires_at_monotonic: float | None = None
    if expires_at:
        from datetime import datetime

        try:
            exp_dt = expires_at
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=UTC)
            if datetime.now(UTC) >= exp_dt:
                _set_api_key_cache(key_hash, None)
                return None
            # 将过期时刻转换为 monotonic 时间戳，供缓存 TTL 上界计算
            remaining_secs = (exp_dt - datetime.now(UTC)).total_seconds()
            expires_at_monotonic = time.monotonic() + remaining_secs
        except (ValueError, TypeError):
            logger.warning("API Key expires_at 值格式无法解析，忽略过期检查: %r", expires_at)

    payload = {"sub": f"apikey:{row['name']}", "via": "apikey"}
    _set_api_key_cache(key_hash, payload, expires_at_ts=expires_at_monotonic)

    # 异步更新 last_used_at（不阻塞，保存引用防止 GC）
    import asyncio

    async def _touch():
        try:
            async with async_session_factory() as s:
                async with s.begin():
                    await ApiKeyRepository(s).touch_last_used(key_hash)
        except Exception:
            logger.exception("更新 API Key last_used_at 失败（非致命）")

    _touch_task = asyncio.create_task(_touch())
    _touch_task.add_done_callback(lambda _: None)  # suppress "never retrieved" warning

    return payload


def _verify_and_get_payload(token: str) -> dict:
    """同步验证 JWT token 并在失败时抛出 401 异常。（仅用于 JWT 路径）"""
    payload = verify_token(token)
    if payload is None:
        raise HTTPException(
            status_code=401,
            detail="token 无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


async def _verify_and_get_payload_async(token: str) -> dict:
    """异步验证 token，支持 API Key（arc- hoặc ）和 JWT 两种模式。"""
    if token.startswith(API_KEY_PREFIX) or token.startswith("arc-"):
        payload = await _verify_api_key(token)
        if payload is None:
            raise HTTPException(
                status_code=401,
                detail="API Key không hợp lệ, đã hết hạn hoặc không tồn tại",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return payload
    if is_logto_configured():
        return verify_logto_token(token)
    # JWT 路径
    return _verify_and_get_payload(token)


def _claim_values(value: object) -> list[str]:
    """Flatten Logto role/scope claim shapes without trusting arbitrary data."""
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = None
            if isinstance(parsed, list):
                return _claim_values(parsed)
        return text.replace(",", " ").replace(";", " ").split()
    if isinstance(value, (list, tuple, set)):
        values: list[str] = []
        for item in value:
            values.extend(_claim_values(item))
        return values
    return []


def _roles_and_scopes(payload: dict[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    role_claims = (
        payload.get("roles"),
        payload.get("role"),
        payload.get("urn:logto:roles"),
        payload.get("http://logto.io/claims/roles"),
        payload.get("http://schemas.microsoft.com/ws/2008/06/identity/claims/role"),
    )
    scope_claims = (payload.get("scope"), payload.get("scopes"), payload.get("scp"))
    roles = tuple(dict.fromkeys(
        item.strip().lower()
        for claim in role_claims
        for item in _claim_values(claim)
        if item.strip()
    ))
    scopes = tuple(dict.fromkeys(
        item.strip().lower()
        for claim in scope_claims
        for item in _claim_values(claim)
        if item.strip()
    ))
    return roles, scopes


def _logto_role(roles: tuple[str, ...], scopes: tuple[str, ...]) -> str:
    candidates = [_LOGTO_ROLE_ALIASES[role] for role in roles if role in _LOGTO_ROLE_ALIASES]
    if "admin:all" in scopes:
        candidates.append("administrator")
    return max(candidates, key=lambda role: _ROLE_ORDER[role]) if candidates else "viewer"


def _payload_to_user(payload: dict[str, Any]) -> CurrentUserInfo:
    """Convert a verified JWT/API-key payload to a scoped CurrentUserInfo.

    Logto identities are keyed by their subject and receive only the highest
    role explicitly present in signed claims. API keys are automation/operator
    identities; neither path silently becomes admin.
    """
    from lib.db.base import DEFAULT_USER_ID

    sub = str(payload.get("sub") or "").strip()
    if not sub:
        raise HTTPException(status_code=401, detail="Authenticated token is missing a subject.")

    via = str(payload.get("via") or "").strip().lower()
    roles, scopes = _roles_and_scopes(payload)
    if via == "apikey":
        role = str(payload.get("role") or "operator").strip().lower()
        if role not in _ROLE_ORDER or _ROLE_ORDER[role] > _ROLE_ORDER["operator"]:
            role = "operator"
        return CurrentUserInfo(
            id=sub[:64],
            sub=sub,
            role=role,
            roles=(role,),
            source="api-key",
        )

    if payload.get("iss"):
        role = _logto_role(roles, scopes)
        return CurrentUserInfo(
            id=sub[:64],
            sub=sub,
            role=role,
            roles=roles,
            scopes=scopes,
            source="logto",
        )

    # The legacy local password flow has one configured operator account and
    # carries an explicit role claim from create_token().
    role = str(payload.get("role") or "").strip().lower()
    if role not in _ROLE_ORDER:
        role = "admin" if sub == os.environ.get("AUTH_USERNAME", "admin") else "operator"
    return CurrentUserInfo(
        id=DEFAULT_USER_ID,
        sub=sub,
        role=role,
        roles=roles or (role,),
        source="legacy",
    )


async def get_current_user(
    token: Annotated[str | None, Depends(oauth2_scheme_optional)] = None,
) -> CurrentUserInfo:
    """标准认证依赖 — 支持 JWT 和 API Key Bearer token。

    ``AUTH_ENABLED=false`` 时无视 token，直接返回匿名 admin。
    启用时缺 token 抛 401（与旧 oauth2_scheme auto_error 行为等价）。
    """
    if not is_auth_enabled():
        return _anonymous_user()
    if not token:
        raise HTTPException(
            status_code=401,
            detail="未认证",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = await _verify_and_get_payload_async(token)
    return _payload_to_user(payload)


async def get_current_operator(
    user: Annotated[CurrentUserInfo, Depends(get_current_user)],
) -> CurrentUserInfo:
    """Require an authenticated operator-level identity."""
    if not user.is_operator:
        raise HTTPException(status_code=403, detail="operator role is required")
    return user


async def get_current_admin(
    user: Annotated[CurrentUserInfo, Depends(get_current_user)],
) -> CurrentUserInfo:
    """Require an explicitly elevated Logto/local administrator identity."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="administrator role is required")
    return user


async def get_current_user_flexible(
    token: Annotated[str | None, Depends(oauth2_scheme_optional)] = None,
    query_token: str | None = Query(None, alias="token"),
) -> CurrentUserInfo:
    """SSE 认证依赖 — 同时支持 Authorization header 和 ?token= query param。

    ``AUTH_ENABLED=false`` 时无视 token，直接返回匿名 admin。
    """
    if not is_auth_enabled():
        return _anonymous_user()
    raw = token or query_token
    if not raw:
        raise HTTPException(
            status_code=401,
            detail="缺少认证 token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = await _verify_and_get_payload_async(raw)
    return _payload_to_user(payload)


# Type aliases for FastAPI dependency injection
CurrentUser = Annotated[CurrentUserInfo, Depends(get_current_user)]
CurrentUserFlexible = Annotated[CurrentUserInfo, Depends(get_current_user_flexible)]
CurrentOperator = Annotated[CurrentUserInfo, Depends(get_current_operator)]
CurrentAdmin = Annotated[CurrentUserInfo, Depends(get_current_admin)]
