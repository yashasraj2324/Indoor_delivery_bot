"""Isolated production-readiness and bootstrap checks."""
from types import SimpleNamespace
import logging
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import app.core.config as config_module
import app.main as main_module
from app.auth.service import register_user
from app.db.models import UserRole


def production_settings(**overrides):
    values = {
        "_env_file": None,
        "APP_ENV": "production",
        "DATABASE_URL": "mysql+aiomysql://dbuser:dbpass@db.example/robots?ssl-mode=VERIFY_IDENTITY",
        "REDIS_URL": "rediss://cache.example/0",
        "JWT_SECRET": "a" * 48,
        "CORS_ORIGINS": "https://dashboard.example",
    }
    values.update(overrides)
    return config_module.Settings(**values)


def test_production_configuration_accepts_tls_and_rejects_unsafe_settings(monkeypatch):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.setattr(config_module, "settings", production_settings())
    assert config_module.validate_production_settings()

    monkeypatch.setattr(config_module, "settings", production_settings(JWT_SECRET="change-me-in-production"))
    with pytest.raises(RuntimeError, match="JWT_SECRET") as error:
        config_module.validate_production_settings()
    assert "dbpass" not in str(error.value)

    monkeypatch.setattr(config_module, "settings", production_settings(REDIS_URL="redis://cache.example/0"))
    with pytest.raises(RuntimeError, match="TLS"):
        config_module.validate_production_settings()

    monkeypatch.setattr(config_module, "settings", production_settings(CORS_ORIGINS="*"))
    with pytest.raises(RuntimeError, match="wildcard"):
        config_module.validate_production_settings()

    monkeypatch.setattr(config_module, "settings", production_settings(ROSBRIDGE_ENABLED=True))
    with pytest.raises(RuntimeError, match="ROSBRIDGE_ENABLED"):
        config_module.validate_production_settings()


def test_robot_command_channel_is_enabled_by_default_and_validates_timeouts():
    defaults = config_module.Settings(_env_file=None)
    assert defaults.ROBOT_COMMAND_CHANNEL_ENABLED is True
    assert defaults.ROBOT_COMMAND_TTL_S == 15
    assert defaults.ROBOT_COMMAND_ACK_TIMEOUT_S == 5

    with pytest.raises(ValueError, match="ROBOT_COMMAND_TTL_S"):
        config_module.Settings(_env_file=None, ROBOT_COMMAND_TTL_S=0)
    with pytest.raises(ValueError, match="ROBOT_COMMAND_ACK_TIMEOUT_S"):
        config_module.Settings(_env_file=None, ROBOT_COMMAND_ACK_TIMEOUT_S=0)


@pytest.mark.asyncio
async def test_public_registration_always_creates_user():
    class Result:
        def scalar_one_or_none(self): return None

    class Session:
        def __init__(self): self.added = []
        async def execute(self, _query): return Result()
        def add(self, item): self.added.append(item)
        async def flush(self): return None

    session = Session()
    user, became_admin = await register_user(session, "new@example.test", "isolated-test-password", "New User")
    assert user.role == UserRole.USER
    assert became_admin is False
    assert session.added[0] is user


@pytest.mark.asyncio
async def test_first_admin_bootstrap_requires_explicit_credentials(monkeypatch):
    class Result:
        def scalar_one_or_none(self): return None

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def execute(self, _query): return Result()
        def add(self, _item): pass
        async def commit(self): pass

    monkeypatch.setattr(main_module, "AsyncSessionLocal", Session)
    monkeypatch.setattr(main_module, "settings", config_module.Settings(_env_file=None))
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="No active ADMIN"):
        await main_module._seed_first_admin()


@pytest.mark.asyncio
async def test_cloud_startup_skips_ros_client_and_keeps_telemetry_unasserted(monkeypatch):
    import sys
    from types import ModuleType

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def execute(self, _query): return 1

    class Redis:
        async def ping(self): return True

    monkeypatch.setattr(main_module, "settings", SimpleNamespace(
        ROSBRIDGE_ENABLED=False, RUN_MIGRATIONS_ON_STARTUP=False,
        ROBOT_COMMAND_CHANNEL_ENABLED=False,
    ))
    monkeypatch.setattr(main_module, "validate_production_settings", lambda: True)
    monkeypatch.setattr(main_module, "AsyncSessionLocal", Session)
    monkeypatch.setattr(main_module, "get_redis", _get_redis(Redis()))
    monkeypatch.setattr(main_module, "_seed_first_admin", _noop)
    from app.safety.manager import safety_manager
    from app.dashboard.service import status_service
    monkeypatch.setattr(safety_manager, "sync_from_db", _noop)
    monkeypatch.setattr(safety_manager, "set_hub", lambda _hub: None)
    monkeypatch.setattr(status_service, "start_monitor", lambda: None)

    ros_module = ModuleType("app.rosbridge.client")
    class ForbiddenRosClient:
        def __init__(self): raise AssertionError("ROS client must not be instantiated")
    ros_module.RosbridgeClient = ForbiddenRosClient
    monkeypatch.setitem(sys.modules, "app.rosbridge.client", ros_module)

    await main_module.on_startup()
    assert not hasattr(main_module.app.state, "rosbridge_client")


@pytest.mark.asyncio
async def test_shutdown_closes_isolated_dependencies_and_monitor(monkeypatch):
    from app.dashboard.service import status_service

    stop_monitor = lambda: None
    close_redis = AsyncMock()
    close_database = AsyncMock()
    monkeypatch.setattr(status_service, "stop_monitor", stop_monitor)
    monkeypatch.setattr(main_module, "close_redis", close_redis)
    monkeypatch.setattr(main_module, "close_database", close_database)
    await main_module.on_shutdown()
    close_redis.assert_awaited_once()
    close_database.assert_awaited_once()


async def _noop(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
async def test_readiness_reports_dependency_failure_and_recovers(monkeypatch):
    state = {"db_ok": True, "redis_ok": False}

    class Session:
        async def __aenter__(self):
            if not state["db_ok"]: raise ConnectionError("isolated DB outage")
            return self
        async def __aexit__(self, *_args): return None
        async def execute(self, _query): return 1

    redis = SimpleNamespace(ping=lambda: _ping(state))
    monkeypatch.setattr(main_module, "AsyncSessionLocal", Session)
    monkeypatch.setattr(main_module, "get_redis", _get_redis(redis))
    with pytest.raises(HTTPException) as unavailable:
        await main_module.readiness()
    assert unavailable.value.status_code == 503
    assert unavailable.value.detail["checks"] == {"database": True, "redis": False}
    assert await main_module.health() == {"status": "ok"}

    state["redis_ok"] = True
    assert await main_module.readiness() == {
        "status": "ready", "checks": {"database": True, "redis": True}
    }
    state["db_ok"] = False
    with pytest.raises(HTTPException) as unavailable:
        await main_module.readiness()
    assert unavailable.value.detail["checks"] == {"database": False, "redis": True}


def test_migration_failure_output_does_not_log_database_credentials(monkeypatch, caplog):
    import app.core.startup as startup_module
    import app.db.session as session_module

    monkeypatch.setattr(session_module, "_normalize_database_url", lambda _url: ("mysql+aiomysql://redacted", {}))
    monkeypatch.setattr(startup_module.shutil, "which", lambda _name: "alembic")
    monkeypatch.setattr(startup_module.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=1, stdout="", stderr="password=do-not-log-this"
    ))
    caplog.set_level(logging.INFO)
    with pytest.raises(RuntimeError) as error:
        startup_module.upgrade_head()
    assert "do-not-log-this" not in str(error.value)
    assert "do-not-log-this" not in caplog.text


def test_auth_rate_limit_ip_ignores_untrusted_forwarded_header():
    from app.auth.router import _client_ip
    request = SimpleNamespace(
        headers={"X-Forwarded-For": "198.51.100.10"},
        client=SimpleNamespace(host="10.0.0.5"),
    )
    assert _client_ip(request) == "10.0.0.5"


def test_rosbridge_endpoint_log_redacts_credentials_and_url_values():
    from app.rosbridge.client import _endpoint_for_log

    redacted = _endpoint_for_log("wss://robot-user:do-not-log@robot.internal:9090/path?token=secret")
    assert redacted == "wss://robot.internal:9090"
    assert "do-not-log" not in redacted and "secret" not in redacted


async def _ping(state):
    if not state["redis_ok"]: raise ConnectionError("isolated Redis outage")
    return True


def _get_redis(client):
    async def get(): return client
    return get
