# AGENTS.md

FastAPI backend for an indoor delivery robot. Single app: `app.main:app`,
one router module per domain under `app/` (auth, admin, delivery, mapping,
dashboard, destinations, safety, events, gateway, nav2, rosbridge). MySQL
(aiomysql) + Redis are required runtime dependencies; startup fails closed
without them. No lint/typecheck config exists — pytest is the only gate.

## Commands (run from repo root)

```bash
pip install -r requirements.txt
pip install aiomysql==0.2.0        # never upgrade; newer aiomysql breaks pool_pre_ping
pip install pytest pytest-asyncio aiosqlite   # test deps are NOT in requirements.txt

uvicorn app.main:app --host 0.0.0.0 --port 8000          # run API (needs .env + MySQL/Redis)
python -m pytest tests/test_deployment_readiness.py tests/test_delivery_lifecycle.py tests/test_robot_gateway.py -q
```

The 3-file command above is the canonical isolated regression suite (no
services needed; lifecycle tests use SQLite). Other isolated test files exist
(`test_gateway_commands.py`, `test_dashboard_ws_auth.py`, `test_robot_gateway.py`,
etc.) but a full `pytest tests` run is **not** guaranteed green — e.g.
`test_session_revocation.py::test_authentication_and_ticket_operations_fail_closed_when_redis_is_down`
currently fails locally, so don't treat a broad-run failure as your regression.

Test tiers:
- `test_e2e_http_sweep.py` / `test_e2e_websocket.py`: need a running API at
  127.0.0.1:8000 plus local MySQL (root, empty password per sweep script) and
  Redis; export `E2E_ADMIN_EMAIL`/`E2E_ADMIN_PASSWORD`. Disposable services
  only, never production.
- `test_session_revocation_redis.py`: opt-in integration; requires
  `TEST_REDIS_URL=redis://127.0.0.1:16379/15` and loops back only — it
  hard-fails on any other host/port/db.
- `test_gateway_failure_cases.py` and `test_delivery_lifecycle.py` are
  self-contained (in-process uvicorn / SQLite / `tools/fake_robot_gateway.py`).

## Environment & config gotchas

- `Settings` reads `.env` from the current working directory — run uvicorn,
  pytest, and alembic from the repo root.
- `RENDER=true` env var triggers the same strict checks as
  `APP_ENV=production` (`app/core/config.py:validate_production_settings`):
  JWT_SECRET 32+ chars, `mysql+aiomysql://` + `ssl-mode=VERIFY_CA|VERIFY_IDENTITY`,
  `rediss://`, no DEBUG/ROSBRIDGE/RUN_MIGRATIONS_ON_STARTUP, no `*` in CORS.
- `redis://` URLs for known TLS-required providers (Upstash, Aiven, Redis
  Cloud, Render) are auto-upgraded to `rediss://`; local URLs are left alone.
- Production validates `ADMIN_EMAIL`/`ADMIN_PASSWORD` as a pair; first admin
  is seeded only when no active admin exists — remove both vars after seeding.
  Public `/auth/register` always creates USER accounts.
- `ROSBRIDGE_ENABLED` is a local-only opt-in (rejected in production); with it
  off, no rosbridge client exists at all. `ROBOT_COMMAND_CHANNEL_ENABLED`
  gates goal commands to robots over the gateway socket.

## Deliveries / navigation boundary

- Without a wired navigation transport, dispatch/pickup-return/cancel return
  HTTP 503 `NAVIGATION_UNAVAILABLE`. The authenticated gateway navigation
  transport is wired by default; set `ROBOT_COMMAND_CHANNEL_ENABLED=false` to
  disable it. `DeliveryStateMachine` remains injectable for tests.
- `app/nav2` and `app/simulation` are not ROS connections to a real robot;
  everything the robot reports is protocol/telemetry, never verified physical
  execution.

## Migrations

- `alembic.ini` leaves `sqlalchemy.url` blank; `alembic/env.py` builds it from
  `DATABASE_URL` and normalizes `ssl-mode=` params (aiomysql rejects them as
  connect kwargs) — see `app/db/session.py:_normalize_database_url`.
- Run in order: `6aba335e` (core schema + raw MySQL `CREATE TRIGGER` — not
  portable to SQLite), then `340c2e71c85c` (user-robot access grants).
- Migrations are a release step, never web-worker startup
  (`RUN_MIGRATIONS_ON_STARTUP=true` only for local single-process dev). MySQL
  DDL may implicitly commit; a failed upgrade can leave partial schema.

## Auth conventions

- Admin guards are dependency factories: write `Depends(require_admin())`
  with the call. `Depends(require_admin)` (uncalled) silently skips the
  check — this exact bug shipped once and was fixed in commit d5f48c8.
- `ROBOT_API_KEY` is a legacy unused setting; the v1 gateway
  (`/ws/robot`) authenticates per-robot bcrypt hashes via `X-Robot-ID` /
  `X-Robot-API-Key` headers. There is no public robot-create endpoint.

## Repo quirks

- Root files `213ui` (pasted git diff) and `test_connections.py` (scratch
  connectivity script) are stray artifacts, not source.
- `docs/` is the authoritative protocol/design source:
  `robot-gateway-protocol.md`, `delivery-lifecycle.md`,
  `mysql-migration-validation.md`, `redis-session-revocation-validation.md`,
  `render-staging-validation.md`, `robot-wifi-rosbridge.md`.
- README's pointer to `.kiro/specs/...` is stale — that directory does not exist.
