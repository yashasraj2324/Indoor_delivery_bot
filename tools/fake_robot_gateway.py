"""SIMULATOR ONLY: fake robot gateway for exercising the backend command channel.

This script pretends to be a robot. It moves no motors, runs no safety
supervisor, and performs no navigation: a "goto" is simulated by sleeping and
then reporting SUCCEEDED. Everything it reports is invented. A passing run
proves only that the backend's WebSocket protocol, state machine and failure
handling behave as coded. It proves NOTHING about a physical robot, its
expiry/limit validation, its safety supervisor, or actual stopping.

Credentials are read from the environment (never argv, which shows up in
process listings): ROBOT_ID, ROBOT_API_KEY, optionally ROBOT_GATEWAY_URL.
--env-file loads KEY=VALUE lines from an untracked file.

Modes:
  default   ack every command; goto -> SUCCEEDED after --goal-seconds,
            stop -> CANCELLED
  --fail    ack, then report FAILED for goto
  --silent  never ack and never report (tests the backend ack timeout)
  --act-delay N  sleep N seconds after receiving a command, before the expiry
            check (tests that a late command is rejected as expired)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import websockets

PROTOCOL_VERSION = 1


def load_env_file(path: str) -> None:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


class FakeRobot:
    def __init__(self, url: str, robot_id: str, api_key: str, args: argparse.Namespace) -> None:
        self.url, self.robot_id, self.api_key, self.args = url, robot_id, api_key, args
        self.ws = None
        self.pose = {"x": 0.0, "y": 0.0, "theta": 0.0}
        self.goal_task: asyncio.Task | None = None
        self._last_ts = datetime.now(timezone.utc)

    def _stamp(self) -> str:
        # Strictly increasing timestamps: the backend rejects out-of-order ones.
        self._last_ts = max(datetime.now(timezone.utc), self._last_ts + timedelta(milliseconds=1))
        return self._last_ts.isoformat()

    async def send(self, type_: str, payload: dict) -> None:
        await self.ws.send(json.dumps({
            "version": PROTOCOL_VERSION, "type": type_, "message_id": str(uuid.uuid4()),
            "robot_id": self.robot_id, "timestamp": self._stamp(), "payload": payload,
        }))

    async def run(self) -> None:
        headers = {"X-Robot-ID": self.robot_id, "X-Robot-API-Key": self.api_key}
        kw = "additional_headers" if int(websockets.__version__.split(".")[0]) >= 14 else "extra_headers"
        async with websockets.connect(self.url, **{kw: headers}) as ws:
            self.ws = ws
            await self.send("hello", {})
            ack = json.loads(await ws.recv())
            print(f"[robot] {ack['type']} {ack['payload']}", flush=True)
            if ack["type"] != "hello_ack":
                return
            tasks = [asyncio.create_task(self._heartbeats()), asyncio.create_task(self._telemetry())]
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") == "command":
                        await self.on_command(msg["payload"])
                    else:
                        print(f"[robot] server -> {msg.get('type')} {msg.get('payload')}", flush=True)
            finally:
                for task in tasks + ([self.goal_task] if self.goal_task else []):
                    task.cancel()

    async def _heartbeats(self) -> None:
        while True:
            await self.send("heartbeat", {})
            await asyncio.sleep(1.0)

    async def _telemetry(self) -> None:
        while True:
            await self.send("telemetry", {
                "mode": "AUTONOMOUS", "safety_state": "SAFE", "controller_connected": True,
                "localization_status": "TRACKING", "pose": dict(self.pose),
                "ultrasonic": [600.0, 600.0], "battery_level": 90.0,
            })
            await asyncio.sleep(1.0)

    async def report(self, type_: str, command_id: str, status: str, detail: str | None = None) -> None:
        payload = {"command_id": command_id, "status": status}
        if detail:
            payload["detail"] = detail[:200]
        print(f"[robot] {type_} {status} {detail or ''}", flush=True)
        await self.send(type_, payload)

    async def on_command(self, cmd: dict) -> None:
        command_id, kind = cmd["command_id"], cmd["kind"]
        print(f"[robot] command {kind} id={command_id[:8]} goal={cmd.get('goal')}", flush=True)
        if self.args.silent:
            print("[robot] --silent: ignoring command", flush=True)
            return
        if self.args.act_delay:
            await asyncio.sleep(self.args.act_delay)
        if datetime.fromisoformat(cmd["expires_at"]) <= datetime.now(timezone.utc):
            await self.report("command_ack", command_id, "REJECTED", "command expired")
            await self.report("command_result", command_id, "FAILED", "command expired")
            return
        await self.report("command_ack", command_id, "ACCEPTED")
        if kind == "stop":
            if self.goal_task:
                self.goal_task.cancel()
            await self.report("command_result", command_id, "CANCELLED", "stop requested")
        elif kind == "goto":
            if self.goal_task:
                self.goal_task.cancel()
            self.goal_task = asyncio.create_task(self._execute_goto(command_id, cmd["goal"]))
        else:
            await self.report("command_result", command_id, "FAILED", "unsupported command")

    async def _execute_goto(self, command_id: str, goal: dict) -> None:
        try:
            await asyncio.sleep(self.args.goal_seconds)
            if self.args.fail:
                await self.report("command_result", command_id, "FAILED", "simulated failure")
                return
            self.pose = {"x": float(goal["x"]), "y": float(goal["y"]), "theta": float(goal["theta"])}
            await self.report("command_result", command_id, "SUCCEEDED")
        except asyncio.CancelledError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="SIMULATOR ONLY: fake robot gateway (proves nothing about hardware)")
    parser.add_argument("--env-file")
    parser.add_argument("--url", default=None)
    parser.add_argument("--goal-seconds", type=float, default=5.0)
    parser.add_argument("--fail", action="store_true", help="report FAILED for goto")
    parser.add_argument("--silent", action="store_true", help="never ack or report commands")
    parser.add_argument("--act-delay", type=float, default=0.0,
                        help="seconds to wait after receiving a command before validating/acting")
    args = parser.parse_args()
    if args.env_file:
        load_env_file(args.env_file)
    robot_id, api_key = os.environ.get("ROBOT_ID"), os.environ.get("ROBOT_API_KEY")
    if not robot_id or not api_key:
        print("ROBOT_ID and ROBOT_API_KEY must be set (env or --env-file)", file=sys.stderr)
        return 2
    url = args.url or os.environ.get("ROBOT_GATEWAY_URL", "ws://localhost:8000/ws/robot")
    print("[robot] SIMULATOR: no physical robot involved", flush=True)
    try:
        asyncio.run(FakeRobot(url, robot_id, api_key, args).run())
    except KeyboardInterrupt:
        pass
    except websockets.exceptions.ConnectionClosed as exc:
        print(f"[robot] connection closed: {exc}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
