# Robot gateway protocol, version 1

The backend exposes `WS /ws/robot`. A gateway authenticates one robot with
`X-Robot-ID` and `X-Robot-API-Key` WebSocket handshake headers. The ID selects
the robot row, and the key is checked against its bcrypt hash. Keys in URL
query parameters are rejected. An ADMIN can create a robot record through
`POST /admin/robots`; the response contains a newly generated gateway API key
once. The database stores only its bcrypt hash, and audit events never include
the key. Copy the key directly into the authorized robot gateway's protected
secret configuration at provisioning time. The API has no read-back or key
rotation endpoint; losing the returned key requires a separately approved
credential replacement procedure.

Every message is a JSON object with this envelope:

```json
{
  "version": 1,
  "type": "hello",
  "message_id": "unique-message-id",
  "robot_id": "database-robot-id",
  "timestamp": "2026-10-14T10:00:00Z",
  "payload": {}
}
```

The timestamp must be ISO 8601 with a timezone. The robot ID must match the
authenticated header. Messages larger than `ROBOT_GATEWAY_MAX_MESSAGE_BYTES`
(default 16384 bytes) are rejected. Unknown fields are rejected in the envelope
and typed telemetry fields.

For a local laptop backend, use `ws://127.0.0.1:8000/ws/robot` when the
gateway runs on the laptop, or the laptop's reachable LAN address when the
gateway runs on a phone. Always send both headers during the WebSocket
handshake; do not put the API key in the URL. The first frame after the socket
opens must be a v1 `hello` envelope with an empty payload, followed by the
`hello_ack` response before sending heartbeat or telemetry frames. A client
that sends a legacy URL query key, omits `hello`, sends a naive timestamp, or
reuses message IDs will be rejected.

## Handshake and telemetry

The first message must be `hello` with an empty payload. The backend replies:

```json
{
  "version": 1,
  "type": "hello_ack",
  "message_id": "server-generated-id",
  "robot_id": "database-robot-id",
  "timestamp": "2026-10-14T10:00:00Z",
  "payload": {"accepted": true}
}
```

Heartbeat payloads may be empty. Telemetry fields are optional; omitted values
remain unknown and do not get filled with assumed healthy states.

```json
{
  "version": 1,
  "type": "telemetry",
  "message_id": "unique-message-id-2",
  "robot_id": "database-robot-id",
  "timestamp": "2026-10-14T10:00:01Z",
  "payload": {
    "mode": "AUTONOMOUS",
    "controller_connected": true,
    "safety_state": "SAFE",
    "localization_status": "TRACKING",
    "pose": {"x": 1.2, "y": 0.4, "theta": 0.0},
    "ultrasonic": [0.82, 1.1, 0.76],
    "diagnostics": {"motor_temperature_c": 34.2},
    "battery_level": 81.5
  }
}
```

Currently persisted into existing columns: `mode`, `safety_state`, and
`battery_level`. Pose is broadcast through the existing dashboard event path;
controller state, localization status, ultrasonic readings, and diagnostics
are included in `robot.telemetry` dashboard events but are not persisted.
`ONLINE`/`STALE`/`OFFLINE` freshness is an additive dashboard/status field so
the existing database enum and REST fields remain compatible. Freshness uses
server receive time; device timestamps are used for ordering, not trusted as
the server's heartbeat clock.

## Protocol-only reports

`command_ack` and `command_result` payloads require a string `command_id`.
They are broadcast as `robot.command_protocol_message` with
`physical_execution_confirmed: false`. No command queue, command dispatch,
ROS action, mode change, reset, or emergency-stop call is connected in this
phase. Do not treat an acknowledgement or a result message as proof of motor
actuation.

`error` messages are accepted as reports and broadcast as
`robot.gateway_error`. Invalid envelopes, mismatched robot IDs, unsupported
versions, repeated message IDs, and out-of-order heartbeat/telemetry messages
are rejected with an `error` message. The backend does not log message bodies,
credentials, or tokens.

The replay/order guard and active-connection registry are process-local. Run a
single backend instance for this phase; a future horizontally scaled Render
deployment needs shared replay/order and connection coordination (for example
Redis atomic operations). Message IDs are retained for 15 minutes (up to 2048
IDs per robot); timestamps more than 30 seconds from server time are rejected.
The heartbeat monitor uses database `last_seen` when the Redis key is absent.
`ROBOT_STALE_AFTER_S` must be less than `HEARTBEAT_TIMEOUT_S`. With defaults,
a connected gateway is `ONLINE` through age 3 seconds, `STALE` after 3 seconds,
and `OFFLINE` after 5 seconds without a heartbeat. The monitor persists the
legacy database `OFFLINE` status only at the 5-second timeout; missing Redis
data alone does not mark the robot offline. A disconnected gateway is reported
`OFFLINE` immediately.

## Later verified safety integration point

Integrate commands only through a dedicated local adapter to the robot's
verified safety supervisor. The adapter must define command authorization,
expiry, idempotency, supervisor acknowledgement, and a timeout/failure path.
It must not replay expired commands after reconnect. Backend acknowledgement
must remain separate from robot-side safety state and verified actuation. No
such adapter is implemented or tested here.

## Optional laptop command channel

Goal commands are enabled by default:

```dotenv
ROBOT_COMMAND_CHANNEL_ENABLED=true
ROBOT_COMMAND_TTL_S=15
ROBOT_COMMAND_ACK_TIMEOUT_S=5
```

Set `ROBOT_COMMAND_CHANNEL_ENABLED=false` to disable dispatch. Use the enabled
channel only for a reviewed, stationary simulator or hardware integration test
after the phone-side executor has been verified.
When enabled, startup wires `DeliveryStateMachine` to the authenticated
gateway adapter; when disabled, delivery dispatch returns
`503 NAVIGATION_UNAVAILABLE` intentionally.

The laptop sends only `goto` and `stop` command envelopes. The phone-side
executor must independently validate command expiry, the `map` frame,
coordinate and heading limits, supervisor health, and duplicate command IDs.
Those protections are not implemented by this repository and must not be
inferred from a command acknowledgement. A host-side stop request is
best-effort; communication-loss stopping is not proven until an independent
phone-side or firmware watchdog is tested.

Safe simulator validation:

1. Set `ROBOT_COMMAND_CHANNEL_ENABLED=false` when normal startup must not send
   goal commands.
2. Use the repository's simulator with a disposable robot/database and set the
   three command-channel variables explicitly only in that test environment.
3. Confirm the simulator sends headers, `hello`, timezone-aware increasing
   timestamps, heartbeat, and telemetry.
4. Exercise acknowledgement timeout, expiry rejection, disconnect cleanup, and
   cancellation using `tests/test_gateway_failure_cases.py`.
5. Do not use this simulator result as evidence of physical navigation, motor
   stopping, or a safety watchdog.
