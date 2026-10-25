# Delivery lifecycle

Delivery creation and history use the existing status values. Dispatch is
ADMIN-only; pickup confirmation is limited to the requester or ADMIN;
cancellation is ADMIN-only. The authenticated robot gateway is the default
navigation transport. Set `ROBOT_COMMAND_CHANNEL_ENABLED=false` to disable it.
State-machine transition records remain available to isolated tests and are
not evidence of physical execution.

| Current state | Request/result | Outcome |
|---|---|---|
| `IDLE` | Dispatch requested with command channel disabled | Remains `IDLE`; HTTP 503 |
| `GO_TO_DESTINATION` | Result supplied to an explicitly injected adapter | `ARRIVED` or failure state |
| `ARRIVED` | Pickup requested with no adapter | Remains `ARRIVED`; HTTP 503 |
| `RETURN_HOME` | Result supplied to an explicitly injected adapter | `HOME` then `IDLE`, or failure state |

Backend state changes must not be described as verified motor actuation or
physical stops.
