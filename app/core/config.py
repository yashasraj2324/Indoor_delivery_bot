"""Application configuration loaded from environment / .env.

Includes automatic `redis://` → `rediss://` upgrade for known
TLS-required Redis providers (Upstash, Aiven, Redis Cloud, Render KV).
Local `redis://` (plaintext) connections are left untouched so that
local Docker development still works.
"""
import os
from urllib.parse import urlparse

from pydantic import AnyUrl, EmailStr, field_validator
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Cloud Redis providers that REQUIRE TLS. The right-hand side is the
# host suffix that uniquely identifies the provider. If a REDIS_URL host
# matches any of these AND the scheme is `redis://`, we auto-upgrade to
# `rediss://` (TLS) so the user doesn't have to remember to add the `s`.
_TLS_REQUIRED_HOST_SUFFIXES: tuple[str, ...] = (
    ".upstash.io",
    "upstash.io",
    ".aivencloud.com",
    ".redis-cloud.com",
    ".render.com",          # Render Key Value Store
    ".redislabs.com",
    ".elastic-cloud.com",
)


def _auto_upgrade_redis_url(url: str) -> str:
    """
    If the URL points at a known TLS-required provider and uses plaintext
    `redis://`, return a copy with `rediss://` so the connection is
    encrypted. Otherwise return the URL unchanged.

    This protects against the common "Connection closed by server" error
    on Upstash / Aiven / etc. when users forget to add the second `s`.
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
    except Exception:
        return url
    if parsed.scheme != "redis":
        return url
    host = (parsed.hostname or "").lower()
    if not host:
        return url
    if any(host == s.lstrip(".") or host.endswith(s) for s in _TLS_REQUIRED_HOST_SUFFIXES):
        return url.replace("redis://", "rediss://", 1)
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Database
    DATABASE_URL: str = "mysql+aiomysql://root:root@localhost:3306/robot_db"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # JWT
    JWT_SECRET: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Robot authentication
    ROBOT_API_KEY: str = "change-me-robot-key"

    # First admin seeding
    ADMIN_EMAIL: EmailStr = "admin@example.com"
    ADMIN_PASSWORD: str = "change-me-admin-password"

    # ROS transport is opt-in; cloud services should leave it disabled.
    ROSBRIDGE_ENABLED: bool = False
    # Local operator-selected ROSBridge target. The admin connection endpoint
    # may replace this in memory for the current process only.
    ROSBRIDGE_URL: str = "ws://localhost:9090"

    # Application
    APP_ENV: str = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    DEBUG: bool = False
    CORS_ORIGINS: str = ""
    RUN_MIGRATIONS_ON_STARTUP: bool = False

    # Safety
    DELIVERY_STATE_TIMEOUT_S: int = 120
    HEARTBEAT_TIMEOUT_S: int = 5
    # Mark telemetry stale before the heartbeat timeout takes the robot offline.
    ROBOT_STALE_AFTER_S: int = 3
    ROBOT_GATEWAY_MAX_MESSAGE_BYTES: int = 16_384
    # Goal commands to the robot over its authenticated gateway socket.
    ROBOT_COMMAND_CHANNEL_ENABLED: bool = True
    ROBOT_COMMAND_TTL_S: float = 15.0
    ROBOT_COMMAND_ACK_TIMEOUT_S: float = 5.0

    @model_validator(mode="after")
    def validate_robot_command_timeouts(self) -> "Settings":
        if self.ROBOT_COMMAND_TTL_S <= 0 or self.ROBOT_COMMAND_TTL_S > 300:
            raise ValueError("ROBOT_COMMAND_TTL_S must be in (0, 300]")
        if self.ROBOT_COMMAND_ACK_TIMEOUT_S <= 0:
            raise ValueError("ROBOT_COMMAND_ACK_TIMEOUT_S must be positive")
        return self

    @model_validator(mode="after")
    def validate_robot_freshness_thresholds(self) -> "Settings":
        if self.HEARTBEAT_TIMEOUT_S <= 0:
            raise ValueError("HEARTBEAT_TIMEOUT_S must be positive")
        if not 0 < self.ROBOT_STALE_AFTER_S < self.HEARTBEAT_TIMEOUT_S:
            raise ValueError("ROBOT_STALE_AFTER_S must be positive and less than HEARTBEAT_TIMEOUT_S")
        return self

    @field_validator("REDIS_URL")
    @classmethod
    def _upgrade_redis_to_tls(cls, v: str) -> str:
        return _auto_upgrade_redis_url(v)


settings = Settings()


def validate_production_settings() -> bool:
    """Validate deployment-only invariants without printing configuration values.

    Render exposes RENDER=true. APP_ENV=production enables the same checks in
    other production environments. Returns whether production mode was found.
    """
    production = settings.APP_ENV.strip().lower() == "production" or os.getenv("RENDER", "").lower() == "true"
    if not production:
        return False

    provided = set(settings.model_fields_set) | set(os.environ)
    required = {"DATABASE_URL", "REDIS_URL", "JWT_SECRET"}
    missing = sorted(required - provided)
    errors = [f"missing required settings: {', '.join(missing)}"] if missing else []
    if len(settings.JWT_SECRET) < 32 or settings.JWT_SECRET == "change-me-in-production":
        errors.append("JWT_SECRET must be a non-default secret of at least 32 characters")
    if settings.JWT_ALGORITHM != "HS256":
        errors.append("JWT_ALGORITHM must be HS256 in production")
    if not settings.DATABASE_URL.startswith("mysql+aiomysql://"):
        errors.append("DATABASE_URL must use mysql+aiomysql in production")
    else:
        query = dict(
            item.split("=", 1) for item in urlparse(settings.DATABASE_URL).query.split("&")
            if "=" in item
        )
        ssl_mode = query.get("ssl-mode", query.get("ssl_mode", "")).upper()
        if ssl_mode not in {"VERIFY_CA", "VERIFY_IDENTITY"}:
            errors.append("DATABASE_URL must use ssl-mode=VERIFY_CA or VERIFY_IDENTITY")
    if not settings.REDIS_URL.startswith("rediss://"):
        errors.append("REDIS_URL must use TLS (rediss://) in production")
    if settings.DEBUG:
        errors.append("DEBUG must be false in production")
    if settings.ROSBRIDGE_ENABLED:
        errors.append("ROSBRIDGE_ENABLED must be false in production")
    if settings.RUN_MIGRATIONS_ON_STARTUP:
        errors.append("RUN_MIGRATIONS_ON_STARTUP must be false; use a single release migration step")
    if "*" in [item.strip() for item in settings.CORS_ORIGINS.split(",") if item.strip()]:
        errors.append("CORS_ORIGINS must not contain wildcard origins in production")
    bootstrap_email = "ADMIN_EMAIL" in provided
    bootstrap_password = "ADMIN_PASSWORD" in provided
    if bootstrap_email != bootstrap_password:
        errors.append("ADMIN_EMAIL and ADMIN_PASSWORD must be supplied together for first-admin provisioning")
    if bootstrap_password and (len(settings.ADMIN_PASSWORD) < 14 or settings.ADMIN_PASSWORD == "change-me-admin-password"):
        errors.append("ADMIN_PASSWORD must be a non-default password of at least 14 characters")
    if errors:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(errors))
    return True
