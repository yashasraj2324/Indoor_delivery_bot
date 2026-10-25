"""Failure-case tests for the robot command channel through the real HTTP API.

A real uvicorn server (ephemeral port, lifespan off) serves ``app.main:app`` in
the test's event loop. Requests go over real HTTP; the simulated robot connects
over a real WebSocket using the simulator's own ``FakeRobot`` client. Stand-ins:
SQLite file instead of MySQL, a minimal Redis double (session-revocation lookups
only) instead of Redis. Everything the robot reports is simulated: these tests
prove protocol/state-machine behaviour only, nothing about physical stopping.
"""
import asyncio
import argparse
import importlib.util
import sys
import time
import uuid
from pathlib import Path

import bcrypt
import httpx
import pytest
import pytest_asyncio
import uvicorn
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.auth.jwt import create_access_token
from app.core.config import settings
from app.db.models import (
    Base, ControlMode, Delivery, DeliveryStatus, Destination, MappingSession,
    MappingSessionStatus, Robot, RobotStatus, User, UserRobotAccess, UserRole,
)
from app.db.redis import get_redis
from app.dashboard.service import RobotStatusService
from app.delivery.state_machine import DeliveryStateMachine
from app.gateway.navigation import GatewayNavigationAdapter

_SIM = Path(__file__).resolve().parent.parent / "tools" / "fake_robot_gateway.py"
_spec = importlib.util.spec_from_file_location("fake_robot_gateway", _SIM)
fake_robot_gateway = importlib.util.module_from_spec(_spec)
sys.modules["fake_robot_gateway"] = fake_robot_gateway
_spec.loader.exec_module(fake_robot_gateway)


class RedisDouble:
    def __init__(self):
        self.values = {}

    async def exists(self, key):
        return int(key in self.values)

    async def setex(self, key, _ttl, value):
        self.values[key] = value

    async def get(self, key):
        return self.values.get(key)


class RecordingRobot(fake_robot_gateway.FakeRobot):
    """Simulator client that records the command kinds it receives."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.seen: list[str] = []
        self.results: list[tuple[str, str, str | None]] = []

    async def on_command(self, cmd):
        self.seen.append(cmd["kind"])
        await super().on_command(cmd)

    async def report(self, type_, command_id, status, detail=None):
        self.results.append((type_, status, detail))
        await super().report(type_, command_id, status, detail)


def _robot_args(**over):
    base = dict(silent=False, fail=False, goal_seconds=30.0, act_delay=0.0)
    base.update(over)
    return argparse.Namespace(**base)


class Stack:
    """Handle to the running server, DB factory and seeded accounts."""

    def __init__(self, factory, port, adapter, admin_hdr, user_hdr, user_id):
        self.factory, self.port, self.adapter = factory, port, adapter
        self.admin, self.user, self.user_id = admin_hdr, user_hdr, user_id
        self.ws_url = f"ws://127.0.0.1:{port}/ws/robot"
        self._robots: list[asyncio.Task] = []

    def client(self):
        return httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}", timeout=30)

    async def provision(self):
        """New robot + finished map + destination + user access. Returns ids and key."""
        key = uuid.uuid4().hex
        robot_id, map_id, dest_id = (str(uuid.uuid4()) for _ in range(3))
        async with self.factory() as db:
            db.add(Robot(id=robot_id, name="r", api_key_hash=bcrypt.hashpw(
                key.encode(), bcrypt.gensalt(rounds=4)).decode()))
            await db.flush()
            db.add(MappingSession(id=map_id, robot_id=robot_id, status=MappingSessionStatus.FINISHED))
            await db.flush()
            db.add(Destination(id=dest_id, name="d", map_id=map_id, x=2.5, y=3.5, theta=0.5, is_home=False))
            db.add(UserRobotAccess(user_id=self.user_id, robot_id=robot_id, granted_by=self.user_id))
            await db.commit()
        return robot_id, key, dest_id

    async def connect_robot(self, robot_id, key, **over):
        robot = RecordingRobot(self.ws_url, robot_id, key, _robot_args(**over))
        task = asyncio.create_task(robot.run())
        self._robots.append(task)
        return robot, task

    async def book_when_ready(self, c, robot_id, dest_id):
        """Book as USER once telemetry has put the robot ONLINE/AUTONOMOUS."""
        for _ in range(60):
            r = await c.post("/deliveries", headers=self.user,
                             json={"destination_id": dest_id, "robot_id": robot_id})
            if r.status_code == 201:
                return r.json()["id"]
            await asyncio.sleep(0.1)
        raise AssertionError(f"could not book: {r.status_code} {r.text}")

    async def status_of(self, c, delivery_id):
        r = await c.get(f"/deliveries/{delivery_id}", headers=self.admin)
        return r.json()["status"]

    async def wait_until(self, predicate, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            await asyncio.sleep(0.02)
        raise AssertionError("condition not reached in time")


@pytest_asyncio.fixture
async def stack(tmp_path, monkeypatch):
    import app.main as main_module  # imports every router/service module

    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}?timeout=30")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    for name, module in list(sys.modules.items()):
        if name.startswith("app.") and hasattr(module, "AsyncSessionLocal"):
            monkeypatch.setattr(module, "AsyncSessionLocal", factory)

    adapter = GatewayNavigationAdapter()
    service = RobotStatusService(hub=sys.modules["app.dashboard.ws_hub"].hub)
    import app.dashboard.router as dashboard_router
    import app.delivery.router as delivery_router
    monkeypatch.setattr(dashboard_router, "gateway_navigation", adapter)
    monkeypatch.setattr(dashboard_router, "status_service", service)
    monkeypatch.setattr(delivery_router, "delivery_state_machine",
                        DeliveryStateMachine(adapter, freshness_check=service.connection_freshness))
    monkeypatch.setattr(settings, "ROBOT_COMMAND_CHANNEL_ENABLED", True)
    monkeypatch.setattr(settings, "ROBOT_COMMAND_ACK_TIMEOUT_S", 1.0)
    monkeypatch.setattr(settings, "ROBOT_COMMAND_TTL_S", 15.0)
    redis_double = RedisDouble()

    async def _get_redis():
        return redis_double

    monkeypatch.setattr(sys.modules["app.dashboard.service"], "get_redis", _get_redis)
    main_module.app.dependency_overrides[get_redis] = _get_redis

    async with factory() as db:
        admin = User(email="admin@t.test", password_hash="x", name="A", role=UserRole.ADMIN)
        user = User(email="user@t.test", password_hash="x", name="U", role=UserRole.USER)
        db.add_all([admin, user])
        await db.commit()
        hdr = lambda u: {"Authorization": "Bearer " + create_access_token(
            str(u.id), u.email, u.role.value, str(uuid.uuid4()))}
        admin_hdr, user_hdr, user_id = hdr(admin), hdr(user), str(user.id)

    server = uvicorn.Server(uvicorn.Config(main_module.app, host="127.0.0.1", port=0,
                                           lifespan="off", log_level="warning"))
    serve = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    s = Stack(factory, port, adapter, admin_hdr, user_hdr, user_id)
    yield s
    for t in s._robots:
        t.cancel()
    await asyncio.gather(*s._robots, return_exceptions=True)
    server.should_exit = True
    await serve
    main_module.app.dependency_overrides.pop(get_redis, None)
    await engine.dispose()


async def _dispatch(c, stack, delivery_id):
    t0 = time.monotonic()
    r = await c.post(f"/deliveries/{delivery_id}/dispatch", headers=stack.admin)
    return r, time.monotonic() - t0


@pytest.mark.asyncio
async def test_control_happy_path_reaches_arrived(stack):
    """Control: the harness can tell success from failure (otherwise the rest could pass vacuously)."""
    rid, key, dest = await stack.provision()
    robot, _ = await stack.connect_robot(rid, key, goal_seconds=0.2)
    async with stack.client() as c:
        did = await stack.book_when_ready(c, rid, dest)
        r, _ = await asyncio.wait_for(_dispatch(c, stack, did), 15)
        assert r.status_code == 200 and r.json()["status"] == "ARRIVED"
        assert robot.seen == ["goto"]


@pytest.mark.asyncio
async def test_case1_silent_robot_fails_after_ack_timeout_and_stop_is_sent(stack):
    rid, key, dest = await stack.provision()
    robot, _ = await stack.connect_robot(rid, key, silent=True)
    async with stack.client() as c:
        did = await stack.book_when_ready(c, rid, dest)
        r, elapsed = await asyncio.wait_for(_dispatch(c, stack, did), 15)
        assert r.status_code == 200
        assert 0.9 <= elapsed < 4, elapsed        # ROBOT_COMMAND_ACK_TIMEOUT_S patched to 1 s
        final = await stack.status_of(c, did)
    assert final == "EMERGENCY_STOP" and final != "ARRIVED"
    await stack.wait_until(lambda: robot.seen == ["goto", "stop"])


@pytest.mark.asyncio
async def test_case2_robot_disconnect_mid_goal_fails_delivery(stack):
    rid, key, dest = await stack.provision()
    robot, robot_task = await stack.connect_robot(rid, key, goal_seconds=60)
    async with stack.client() as c:
        did = await stack.book_when_ready(c, rid, dest)
        dispatch = asyncio.create_task(_dispatch(c, stack, did))
        await stack.wait_until(lambda: any(s == "ACCEPTED" for _t, s, _d in robot.results))
        robot_task.cancel()                          # drops the websocket
        r, _ = await asyncio.wait_for(dispatch, 15)
        assert r.status_code == 200
        assert await stack.status_of(c, did) == "EMERGENCY_STOP"
    async with stack.factory() as db:
        delivery = (await db.execute(select(Delivery).where(Delivery.id == did))).scalar_one()
    assert any("robot gateway disconnected" in h["reason"] for h in delivery.state_history_json)


@pytest.mark.asyncio
async def test_case3_expired_command_is_rejected_by_robot_and_delivery_fails(stack, monkeypatch):
    # Robot acts after the 1 s TTL but before the (5 s) ack timeout, so the failure
    # must come from the robot's expiry rejection, not from the ack watchdog.
    monkeypatch.setattr(settings, "ROBOT_COMMAND_TTL_S", 1.0)
    monkeypatch.setattr(settings, "ROBOT_COMMAND_ACK_TIMEOUT_S", 5.0)
    rid, key, dest = await stack.provision()
    robot, _ = await stack.connect_robot(rid, key, act_delay=1.5)
    async with stack.client() as c:
        did = await stack.book_when_ready(c, rid, dest)
        r, _ = await asyncio.wait_for(_dispatch(c, stack, did), 15)
        assert r.status_code == 200
        assert await stack.status_of(c, did) == "EMERGENCY_STOP"
    assert ("command_ack", "REJECTED", "command expired") in robot.results
    assert ("command_result", "FAILED", "command expired") in robot.results
    async with stack.factory() as db:
        delivery = (await db.execute(select(Delivery).where(Delivery.id == did))).scalar_one()
    assert any("command expired" in h["reason"] for h in delivery.state_history_json)


@pytest.mark.asyncio
async def test_case4a_dispatch_after_robot_disconnected_is_409_not_hang(stack):
    rid, key, dest = await stack.provision()
    robot, robot_task = await stack.connect_robot(rid, key)
    async with stack.client() as c:
        did = await stack.book_when_ready(c, rid, dest)
        robot_task.cancel()
        await stack.wait_until(lambda: not stack.adapter.is_connected(rid))
        await asyncio.sleep(0.3)                     # let the server mark the robot OFFLINE
        r, elapsed = await asyncio.wait_for(_dispatch(c, stack, did), 10)
        assert r.status_code == 409 and r.json()["detail"]["error"] == "ROBOT_OFFLINE"
        assert elapsed < 3
        assert await stack.status_of(c, did) == "IDLE"
    assert robot.seen == []


@pytest.mark.asyncio
async def test_case4b_db_says_online_but_no_gateway_connection_is_409(stack):
    rid, key, dest = await stack.provision()
    robot, robot_task = await stack.connect_robot(rid, key)
    async with stack.client() as c:
        did = await stack.book_when_ready(c, rid, dest)
        robot_task.cancel()
        await stack.wait_until(lambda: not stack.adapter.is_connected(rid))
        await asyncio.sleep(0.3)
        async with stack.factory() as db:            # stale DB row claiming ONLINE
            await db.execute(update(Robot).where(Robot.id == rid).values(
                status=RobotStatus.ONLINE, control_mode=ControlMode.AUTONOMOUS))
            await db.commit()
        r, elapsed = await asyncio.wait_for(_dispatch(c, stack, did), 10)
        assert r.status_code == 409 and r.json()["detail"]["error"] == "INVALID_DELIVERY_STATE"
        assert elapsed < 3
        assert await stack.status_of(c, did) == "IDLE"


@pytest.mark.asyncio
async def test_case5_cancel_mid_goal_sends_stop_and_ends_emergency_stop(stack):
    rid, key, dest = await stack.provision()
    robot, _ = await stack.connect_robot(rid, key, goal_seconds=60)
    async with stack.client() as c:
        did = await stack.book_when_ready(c, rid, dest)
        dispatch = asyncio.create_task(_dispatch(c, stack, did))
        await stack.wait_until(lambda: any(s == "ACCEPTED" for _t, s, _d in robot.results))
        cancel = await c.post(f"/deliveries/{did}/cancel", headers=stack.admin)
        assert cancel.status_code == 200
        assert cancel.json()["status"] == "EMERGENCY_STOP"
        assert cancel.json()["cancellation_requested"] is True
        r, _ = await asyncio.wait_for(dispatch, 15)
        assert r.status_code == 200 and r.json()["status"] == "EMERGENCY_STOP"
        assert await stack.status_of(c, did) == "EMERGENCY_STOP"
    await stack.wait_until(lambda: robot.seen == ["goto", "stop"])


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["dispatch", "cancel"])
async def test_case6_auth_rejects_missing_invalid_and_non_admin_tokens(stack, action):
    rid, key, dest = await stack.provision()
    async with stack.factory() as db:
        delivery = Delivery(robot_id=rid, destination_id=dest, requester_id=stack.user_id,
                            status=DeliveryStatus.IDLE, state_history_json=[])
        db.add(delivery)
        await db.commit()
        did = str(delivery.id)
    url = f"/deliveries/{did}/{action}"
    async with stack.client() as c:
        assert (await c.post(url)).status_code == 401
        assert (await c.post(url, headers={"Authorization": "Bearer not.a.jwt"})).status_code == 401
        forbidden = await c.post(url, headers=stack.user)
        assert forbidden.status_code == 403
        assert forbidden.json()["detail"]["error"] == "FORBIDDEN"
        assert await stack.status_of(c, did) == "IDLE"      # nothing was dispatched/cancelled
