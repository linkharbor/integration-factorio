#!/usr/bin/env python3
"""LinkHarbor agent for Factorio servers.

Connects to the server's RCON port (Source RCON protocol, default 27015),
runs `/players online`, `/time` and `/config get max-players`, and pushes the
state to a LinkHarbor "Custom (API)" integration: a live game-server card on
your bio page (players, a "Join in game" button for Steam, optionally the map
time or your own text with placeholders) and push notifications for joins.

    export LH_INGEST_URL="https://dock.at-harbor.com/v1/ingest/123"
    export LH_INGEST_SECRET="…"
    export FACTORIO_RCON_PASSWORD="…"
    python3 agent.py --host 203.0.113.10 --port 27015 --name "Our Factory" \
        --address "203.0.113.10:34197" --url "https://discord.gg/xyz"

No third-party packages needed (Python 3.10+, standard library only).

Events sent (each can drive a push rule in the dashboard):
    server.online    the server answered after being unreachable (or on first run)
    server.offline   the server stopped answering
    player.joined    data.player = name
    player.left      data.player = name
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid

log = logging.getLogger("linkharbor")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", required=True, help="server IP / hostname")
    p.add_argument("--port", type=int, default=27015, help="RCON port (default 27015)")
    p.add_argument("--password", default=os.environ.get("FACTORIO_RCON_PASSWORD", ""),
                   help="RCON password (or env FACTORIO_RCON_PASSWORD)")
    p.add_argument("--name", default="Factorio server", help='card title (default "Factorio server")')
    p.add_argument("--address", default="", help="address shown on the card, e.g. 203.0.113.10:34197")
    p.add_argument("--url", default="", help="link behind the card's button (Discord, website)")
    p.add_argument("--cta", default="Join server", help="button label")
    p.add_argument("--image-url", default="", help="banner image for the large layout")
    p.add_argument("--max-players", type=int, default=None,
                   help="player limit shown as n / max (default: read from the server, 0 = no limit)")
    p.add_argument("--no-join", action="store_true",
                   help="no \"Join in game\" button (default: one for Steam when --address is set)")
    p.add_argument("--interval", type=int, default=60, help="seconds between polls (default 60)")
    p.add_argument("--heartbeat", type=int, default=10, help="minutes between state-only updates (0 = only on change)")
    p.add_argument("--no-player-names", action="store_true", help="don't send player names")
    p.add_argument("--map-time", action="store_true", help='card text "Map time: …" (the /time output)')
    p.add_argument("--description", default="",
                   help="card text with placeholders, e.g. \"{players} of {max} online · {time} on the map\" (see README)")
    p.add_argument("--var", action="append", default=[], metavar="KEY=VALUE",
                   help="your own placeholder {KEY} for --description (repeatable)")
    p.add_argument("--ingest-url", default=os.environ.get("LH_INGEST_URL", ""))
    p.add_argument("--ingest-secret", default=os.environ.get("LH_INGEST_SECRET", ""))
    p.add_argument("--once", action="store_true", help="poll once, send, exit")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()
    if not a.ingest_url or not a.ingest_secret:
        p.error("set LH_INGEST_URL and LH_INGEST_SECRET (or --ingest-url / --ingest-secret)")
    if not a.password:
        p.error("set FACTORIO_RCON_PASSWORD (or --password)")
    a.vars = {}
    for item in a.var:
        key, sep, value = item.partition("=")
        key = key.strip().lower()
        if not sep or not re.fullmatch(r"[a-z0-9_]{1,40}", key):
            p.error(f"--var {item!r}: use KEY=VALUE, KEY with a-z, 0-9 and _")
        if key in BUILTIN_PLACEHOLDERS:
            p.error(f"--var {key}: {{{key}}} is filled by the agent itself")
        a.vars[key] = value.strip()
    unknown = sorted(set(re.findall(r"\{([a-z0-9_]+)\}", a.description)) - set(BUILTIN_PLACEHOLDERS) - set(a.vars))
    if unknown:
        p.error("--description: unknown placeholder(s) " + ", ".join("{" + u + "}" for u in unknown)
                + " (built in: " + ", ".join("{" + k + "}" for k in BUILTIN_PLACEHOLDERS) + "; add your own with --var)")
    return a


class Ingest:
    def __init__(self, url: str, secret: str) -> None:
        self.url, self.secret = url, secret

    def send(self, event: str | None, data: dict | None, state: dict) -> bool:
        body: dict = {"state": state}
        if event:
            body["event"] = event
            body["event_id"] = f"{event}-{uuid.uuid4()}"
        if data:
            body["data"] = data
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.secret}", "Content-Type": "application/json",
                     "User-Agent": "LinkHarbor-Factorio/1.0"},
        )
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(req, timeout=10) as res:
                    log.debug("← %s %s", res.status, res.read(200).decode(errors="replace"))
                    return True
            except urllib.error.HTTPError as e:
                text = e.read(300).decode(errors="replace")
                if e.code == 401:
                    log.error("401 from LinkHarbor — wrong secret or integration disconnected")
                    return False
                if e.code < 500:
                    log.error("%s from LinkHarbor: %s", e.code, text)
                    return False
                log.warning("%s from LinkHarbor (attempt %d)", e.code, attempt)
            except Exception as e:  # network
                log.warning("request failed (attempt %d): %s", attempt, e)
            time.sleep(2)
        return False


# --- Source RCON ------------------------------------------------------------
# Packet: int32 size (of everything after it), int32 id, int32 type, body, \0, \0.
# Types: 3 = auth, 2 = exec command (and the auth response), 0 = response.
# A failed auth is answered with id -1.

RCON_AUTH, RCON_EXEC, RCON_RESPONSE = 3, 2, 0


class RconAuthError(Exception):
    pass


def _rcon_send(sock: socket.socket, pid: int, ptype: int, body: str) -> None:
    payload = struct.pack("<ii", pid, ptype) + body.encode() + b"\x00\x00"
    sock.sendall(struct.pack("<i", len(payload)) + payload)


def _rcon_read(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed")
        buf += chunk
    return buf


def _rcon_recv(sock: socket.socket) -> tuple[int, int, str]:
    (size,) = struct.unpack("<i", _rcon_read(sock, 4))
    if size < 10 or size > 1_048_576:
        raise ValueError(f"bad packet size {size}")
    payload = _rcon_read(sock, size)
    pid, ptype = struct.unpack("<ii", payload[:8])
    return pid, ptype, payload[8:-2].decode(errors="replace")


def rcon(host: str, port: int, password: str, commands: list[str], timeout: float = 5.0) -> list[str]:
    """Open a connection, authenticate, run the commands, return their outputs."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        _rcon_send(sock, 1, RCON_AUTH, password)
        while True:  # some servers send an empty RESPONSE before the auth reply
            pid, ptype, _ = _rcon_recv(sock)
            if ptype == RCON_EXEC:
                break
        if pid == -1:
            raise RconAuthError("RCON password rejected")
        out: list[str] = []
        for i, cmd in enumerate(commands, start=2):
            _rcon_send(sock, i, RCON_EXEC, cmd)
            while True:
                pid, ptype, body = _rcon_recv(sock)
                if pid == i:
                    break
            out.append(body)
        return out


# --- Factorio ----------------------------------------------------------------

def parse_players(text: str) -> tuple[int, list[str]]:
    """`/players online` → "Online players (2):\\n  alice (online)\\n  bob (online)"."""
    lines = text.strip().splitlines()
    m = re.search(r"\((\d+)\)", lines[0]) if lines else None
    count = int(m.group(1)) if m else 0
    names: list[str] = []
    for line in lines[1:]:
        name = re.sub(r"\s*\(online\)\s*$", "", line.strip())
        if name:
            names.append(name)
    return max(count, len(names)), names


def parse_max_players(text: str) -> int:
    """`/config get max-players` → "0" (no limit) or "16"; anything else → 0."""
    m = re.search(r"(\d+)\s*$", text.strip())
    return int(m.group(1)) if m else 0


def join_url(address: str) -> str:
    """Steam link that starts Factorio and connects (`--mp-connect host:port`), or ""."""
    m = re.fullmatch(r"([A-Za-z0-9.-]{1,253})(?::(\d{1,5}))?", address.strip())
    if not m or not (0 < int(m.group(2) or 34197) <= 65535):
        return ""
    return f"steam://run/427520//--mp-connect%20{m.group(1)}:{m.group(2) or 34197}/"


def query(host: str, port: int, password: str) -> tuple[dict | None, list[str]]:
    """Returns (info, player names) or (None, []) when the server is unreachable."""
    try:
        players_out, time_out, max_out = rcon(host, port, password, ["/players online", "/time", "/config get max-players"])
    except RconAuthError as e:
        log.error("%s", e)
        return None, []
    except Exception as e:
        log.debug("rcon failed: %s", e)
        return None, []
    count, names = parse_players(players_out)
    return {"players": count, "time": " ".join(time_out.split()), "max": parse_max_players(max_out)}, names


#: Placeholders the agent fills in --description itself.
BUILTIN_PLACEHOLDERS = ("name", "players", "max", "time", "address", "player_list", "status")


def describe(a: argparse.Namespace, info: dict | None, names: list[str]) -> str:
    """The card text: --description with its placeholders filled, else
    "Map time: …" with --map-time, else nothing (at most 200 characters)."""
    if a.description:
        limit = a.max_players if a.max_players is not None else (info or {}).get("max", 0)
        values = {
            "name": a.name,
            "players": str((info or {}).get("players", 0)),
            "max": str(limit) if limit else "",
            "time": (info or {}).get("time", ""),
            "address": a.address,
            "player_list": ", ".join(names) if info and not a.no_player_names else "",
            "status": "online" if info else "offline",
            **a.vars,
        }
        text = re.sub(r"\{([a-z0-9_]+)\}", lambda m: values.get(m.group(1), m.group(0)), a.description)
        return " ".join(text.split())[:200]
    if a.map_time and info and info.get("time"):
        return f"Map time: {info['time']}"[:200]
    return ""


def build_state(a: argparse.Namespace, info: dict | None, names: list[str]) -> dict:
    s: dict = {"type": "game_server", "name": a.name, "game": "Factorio"}
    if a.address:
        s["address"] = a.address
    if a.url:
        s["url"] = a.url
    if a.cta:
        s["cta"] = a.cta
    if a.image_url:
        s["image_url"] = a.image_url
    if a.address and not a.no_join and join_url(a.address):
        s["join_url"] = join_url(a.address)
    text = describe(a, info, names)
    if text:
        s["description"] = text
    if info is None:
        s.update(online=False, players_online=0, players=[])
        return s
    s.update(
        online=True,
        players_online=info["players"],
        players_max=a.max_players if a.max_players is not None else info.get("max", 0),
        players=[] if a.no_player_names else names[:50],
    )
    return s


def main() -> int:
    a = parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ingest = Ingest(a.ingest_url, a.ingest_secret)

    online: bool | None = None
    known: set[str] = set()
    last_sent = 0.0
    heartbeat_s = a.heartbeat * 60

    while True:
        info, names = query(a.host, a.port, a.password)
        now_online = info is not None
        state = build_state(a, info, names)
        current = set(names)

        events: list[tuple[str, dict | None]] = []
        if online is None or now_online != online:
            events.append(("server.online" if now_online else "server.offline", None))
        if now_online and online is not None and not a.no_player_names:
            for n in sorted(current - known):
                events.append(("player.joined", {"player": n}))
            for n in sorted(known - current):
                events.append(("player.left", {"player": n}))

        if events:
            for ev, data in events:
                log.info("%s %s", ev, (data or {}).get("player", ""))
                ingest.send(ev, data, state)
            last_sent = time.time()
        elif heartbeat_s and time.time() - last_sent >= heartbeat_s:
            log.info("heartbeat: %s players online", state.get("players_online", 0))
            ingest.send(None, None, state)
            last_sent = time.time()

        online = now_online
        known = current if now_online else set()
        if a.once:
            return 0
        time.sleep(max(5, a.interval))


if __name__ == "__main__":
    sys.exit(main())
