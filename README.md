# Indoor Delivery Robot Backend

FastAPI + MySQL + Redis backend for the Autonomous Indoor Delivery Robot system.

See `.kiro/specs/indoor-delivery-robot-backend/` for the full requirements, design, and task breakdown.

## Local development

```bash
# 1. Copy env template and edit (DB URL, Redis URL, secrets)
cp .env.example .env

# 2. Install deps
pip install -r requirements.txt
pip install aiomysql==0.2.0      # required version (newer breaks pool_pre_ping)

# 3. Run isolated regression tests
python -m pytest tests/test_deployment_readiness.py tests/test_delivery_lifecycle.py tests/test_robot_gateway.py -q
```

The `test_e2e_*.py` scripts contact a running local API and MySQL/Redis; use
only disposable local services for those tests, never production.

Schema migrations are a release step, not a web-worker startup action. Run
`alembic upgrade head` once against the intended database before deploying new
application instances. Set `RUN_MIGRATIONS_ON_STARTUP=true` only for local,
single-process development.

For a fresh local database, set `ADMIN_EMAIL` and `ADMIN_PASSWORD` in `.env`
before the first app start; remove them after the admin has been seeded.
Public registration creates USER accounts only.

## Run locally

```bash
# Start MySQL + Redis (Docker)
docker run -d --name mysql -e MYSQL_ROOT_PASSWORD=root -e MYSQL_DATABASE=robot_db -p 3306:3306 mysql:8.0
docker run -d --name redis -p 6379:6379 redis:7-alpine

# Start the API
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000/docs for the Swagger UI.

## Deploy to Render

The Dockerfile is the build entry point. Run schema migrations once as a
controlled release step before starting web workers; the app does not migrate
on startup by default.

### 1. Provision external services (free tiers work)

| Service | Provider | Notes |
|---|---|---|
| MySQL | Managed MySQL provider | Use `mysql+aiomysql://USER:PASS@HOST:3306/robot_db?ssl-mode=VERIFY_IDENTITY` or `VERIFY_CA`. |
| Redis | Managed Redis provider | Use its TLS URL, normally `rediss://...`. |

### 2. Create a Render Web Service

- **Source**: connect your GitHub repo
- **Runtime**: Docker (Render will detect the `Dockerfile` automatically)
- **Region**: any
- **Instance type**: Free (Starter) for dev

### 3. Set environment variables on the Web Service

```
APP_ENV=production
DATABASE_URL=mysql+aiomysql://USER:PASS@HOST:3306/robot_db?ssl-mode=VERIFY_IDENTITY
REDIS_URL=rediss://USER:PASS@HOST:6379/0
JWT_SECRET=<generate with: openssl rand -hex 32>
ROSBRIDGE_ENABLED=false
DEBUG=false
RUN_MIGRATIONS_ON_STARTUP=false
CORS_ORIGINS=https://your-dashboard.example
```

For the initial administrator only, set `ADMIN_EMAIL` and `ADMIN_PASSWORD`
(unique, at least 14 characters). The app seeds that account only if no active
administrator exists; remove both variables after the first successful startup.
Public `/auth/register` always creates a USER. Add later administrators through
the authenticated `/admin/users` endpoint.

### Robot and delivery execution boundary

The backend has no mock navigation or simulation runtime. Dispatch,
pickup-return navigation, and cancellation use the authenticated robot gateway
when `ROBOT_COMMAND_CHANNEL_ENABLED=true` (the default); set it to `false` to
disable those operations. The delivery state machine remains injectable for
isolated tests. The `/sim` and `/robot/manual-control` endpoints are not
provided. `ROSBRIDGE_ENABLED` defaults to false; this repository does not
verify physical navigation, motor stop, or robot safety.

### Database migration controls

Migrations must run in order: `6aba335e` creates the core schema and the
append-only `events` triggers; `340c2e71c85c` adds robot access grants. Run
`alembic upgrade head` once per release from a controlled migration runner,
using the same verified-TLS database URL as the application. The migration
identity needs database connection plus CREATE/ALTER/DROP TABLE, CREATE/DROP
INDEX, REFERENCES, and TRIGGER privileges. The initial revision executes raw
MySQL `CREATE TRIGGER` statements and is not portable to SQLite. Take and verify
a restorable backup before migration. MySQL DDL may implicitly commit, so a
failed upgrade can leave partial schema changes; inspect the schema and Alembic
version table before retrying. Downgrading the initial revision drops core
tables and data and is not a routine rollback strategy; use a forward fix or
restore a verified backup after an approved recovery decision.
For a repeatable fresh-schema check against a disposable local MySQL container,
see [`docs/mysql-migration-validation.md`](docs/mysql-migration-validation.md).

### Robot gateway credential operations

There is no public robot-create or raw-key endpoint. An authorized operator
should run a reviewed, access-controlled provisioning script in a trusted
environment: generate a high-entropy key with a cryptographic random source,
store only its bcrypt hash in `robots.api_key_hash`, create the robot with
`status=OFFLINE`, `control_mode=STOPPED`, and `slam_state=IDLE`, and deliver the
raw key once through the organization's secret manager. Do not put the key in
SQL history, shell arguments, logs, tickets, or chat. Restrict the database
operator role and audit the provisioned robot ID and operator separately.

For rotation, generate a new key and atomically replace the stored bcrypt hash,
then update the gateway's secret and reconnect it. Revoke by replacing the
hash with a hash of a newly generated undistributed key and stopping the
gateway credential holder. The backend authenticates at WebSocket connection
time; it does not currently terminate an already authenticated socket when a
hash changes. Treat active-session closure as part of the operator procedure.
Never set a robot ONLINE or mark localization healthy as part of provisioning.

Run `alembic upgrade head` once as a controlled release step using the same
database URL, then deploy/start web workers. The initial migration creates
MySQL append-only triggers, so verify DDL/trigger permissions and take a backup
before schema changes. Do not enable startup migrations on multiple instances.
Render supplies `PORT`; the container falls back to `APP_PORT` and then 8000
for local runs. Startup checks database and Redis connectivity and fails closed
if configuration or required dependencies are invalid.

### 5. Verify

```
curl https://your-service.onrender.com/health
# → {"status":"ok"}
```

### Troubleshooting

| Symptom | Fix |
|---|---|
| `Application startup failed. Exiting` in logs | Check the startup stage, production configuration, and whether MySQL and Redis are reachable using the configured TLS URLs. The application logs the failure stage without printing connection strings. |
| Redis connection failure | Confirm `REDIS_URL` is the provider's TLS `rediss://` URL and that the service permits connections from the Render service. |
| `AssertionError: Status code 204 must not have a response body` | Should not happen on the current main — all 204 routes have `response_class=Response` |
| 500 on `/auth/login` after a few attempts | You're being rate-limited. Wait 15 minutes or flush the Redis bucket. |

## API Endpoints

Swagger UI at `/docs` (auto-generated).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | Liveness check |
| POST | `/auth/login` | none | Email+password → access token + refresh cookie |
| POST | `/auth/register` | none | Public self-registration as USER |
| POST | `/auth/refresh` | cookie | New access token |
| POST | `/auth/logout` | bearer | Revoke the current access/refresh session |
| GET | `/auth/me` | bearer | Current user profile |
| GET | `/auth/me/robots` | bearer | Robots the current user has been granted access to |
| POST | `/admin/users` | ADMIN | Create user |
| GET | `/admin/users` | ADMIN | List users |
| PATCH | `/admin/users/{id}` | ADMIN | Update role / is_active |
| DELETE | `/admin/users/{id}` | ADMIN | Soft-delete |
| POST | `/admin/users/{id}/robots/{rid}` | ADMIN | Grant user access to a robot |
| DELETE | `/admin/users/{id}/robots/{rid}` | ADMIN | Revoke access |
| GET | `/admin/users/{id}/robots` | ADMIN | List a user's robot access |
| POST/GET | `/mapping/sessions[/...]` | ADMIN | SLAM mapping session lifecycle |
| GET | `/robot/status` | ADMIN | Robot snapshot |
| GET | `/destinations` | ADMIN | Destination CRUD |
| POST/GET | `/deliveries` | USER+access | Book a delivery on a robot you're granted access to |
| POST | `/robot/mode` | ADMIN | Switch control mode |
| POST | `/robot/emergency-stop` | ADMIN | Trigger e-stop |
| POST | `/robot/emergency-stop/reset` | ADMIN | Reset e-stop |
| GET | `/robot/safety-state` | ADMIN | Current safety state |
| GET | `/events` | ADMIN | Audit log (filterable, paginated) |
| POST | `/dashboard/ws-ticket` | ADMIN bearer JWT | Create a 30-second, one-use dashboard WebSocket ticket |
| WS | `/ws/dashboard?ticket=...` | ADMIN one-use ticket | Real-time robot events |
| WS | `/ws/robot` | `X-Robot-ID` and `X-Robot-API-Key` headers | Versioned inbound robot gateway telemetry |

## Architecture

```
Phone gateway (planned)  ──WSS──▶  FastAPI Backend
                                       ├── MySQL  (persistent state)
                                       └── Redis  (blacklist, rate limit, heartbeat)
```

The Phase 1 backend does not connect to ROS or the physical robot. The phone
gateway and a verified local safety-supervisor adapter are later integration
work.

## Robot gateway protocol (Phase 1)

`/ws/robot` authenticates a specific database robot using the `X-Robot-ID`
and `X-Robot-API-Key` headers. The key is checked against that robot's stored
bcrypt hash. Credentials in query parameters are rejected. After connecting,
the gateway must send a version 1 `hello` envelope before heartbeat or
telemetry messages. The complete message contract and examples are in
[`docs/robot-gateway-protocol.md`](docs/robot-gateway-protocol.md).

Supported inbound message types are `hello`, `heartbeat`, `telemetry`,
`command_ack`, `command_result`, and `error`. Command acknowledgement/result
messages are protocol reports only; this backend phase has no command sender
or ROS adapter and does not infer physical execution from them.

## Render environment and health checks

Configure these variables in the Render service environment (values are
managed in Render and must not be committed):

- Required in production: `DATABASE_URL` (MySQL TLS), `REDIS_URL` (TLS), and
  `JWT_SECRET` (32+ characters, not a default). `ADMIN_EMAIL` and
  `ADMIN_PASSWORD` are needed only for first-admin bootstrap when no active
  admin exists.
- Optional authentication tuning: `JWT_ACCESS_TOKEN_EXPIRE_MINUTES`,
  `JWT_REFRESH_TOKEN_EXPIRE_DAYS`.
- Optional gateway/runtime tuning: `ROSBRIDGE_ENABLED` (default false; keep
  false on Render), `HEARTBEAT_TIMEOUT_S` (default 5),
  `ROBOT_STALE_AFTER_S` (default 3),
  `ROBOT_GATEWAY_MAX_MESSAGE_BYTES` (default 16384),
  `DEBUG`, `APP_HOST`, and `APP_PORT`.
- Render supplies `PORT`; the Docker command binds to it, falling back to
  `APP_PORT` and then 8000 for local runs.
- `APP_ENV=production` enables strict configuration checks. `CORS_ORIGINS` is a comma-separated allowlist; production rejects `*`.
- `RUN_MIGRATIONS_ON_STARTUP` defaults to false. Use a single controlled release migration step before deploying workers.
- Delivery execution routes return `NAVIGATION_UNAVAILABLE` until a real
  navigation transport is reviewed and integrated.
- `ROBOT_API_KEY` remains as a legacy setting but is not used by the v1
  gateway. Gateway authentication uses each robot's database bcrypt hash.

Dashboard browsers obtain a 30-second, single-use ticket from the
bearer-authenticated `POST /dashboard/ws-ticket` endpoint and connect to
`/ws/dashboard?ticket=...`. A long-lived access JWT is no longer accepted in a
WebSocket URL. The ticket is still a short query parameter, so proxies must
avoid recording query strings or redact the `ticket` parameter. Configure the
dashboard client to use this exchange; do not log the response ticket.
Access and refresh tokens created by one login share an opaque session ID.
Logout writes a Redis revocation marker for that session and blacklists its
presented refresh token. Authenticated HTTP requests, refresh, ticket issuance,
and ticket redemption fail closed when Redis cannot verify revocation. Tickets
are bound to both the admin and session, and cannot be redeemed after logout.
Existing dashboard sockets poll the session marker and close on logout within
about one second under normal scheduling; Redis failure closes them with a
retryable WebSocket error. This is bounded polling, not instantaneous socket
termination. Access and refresh JWTs issued before session IDs were introduced
must be replaced by logging in again after upgrading.

With `ROSBRIDGE_ENABLED=false`, startup does not import, instantiate, or start
RosbridgeClient. Robot connection and localization remain telemetry-driven;
disabling ROS does not set either state healthy. Enabling the bridge is an
explicit local integration opt-in and is rejected in production configuration.

Gateway connection/replay state is process-local. Phase 1 supports one backend
instance; scaling needs shared connection/replay coordination and one migration
runner. Delivery dispatch uses database row locks for cross-instance claims,
but motion readiness also depends on each process's gateway freshness state.
Map references currently store only `map_data_ref`; future map files should go
to durable object storage (such as an S3-compatible bucket), with only the
object key, checksum, and metadata stored in MySQL. No map files are currently
written to local disk.

MySQL and Redis remain required application dependencies. `/health` is a
liveness response only. `/ready` checks MySQL (`SELECT 1`) and Redis (`PING`)
and returns HTTP 503 with dependency check booleans if either is unavailable.
Robot connectivity is intentionally not a readiness dependency because the
phone gateway and robot are not required to boot the backend.
