#!/usr/bin/env python3
#
# Copyright (C) 2026  Mathieu Réquillart
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Snapcast control script for librespot, fed by librespot's --onevent hook.

One file, two roles, told apart by PLAYER_EVENT in the environment:
- hook: run by librespot once per player event; forwards it over a Unix socket.
- control script: run by snapserver; turns those events into stream properties.

Each stream gets its own socket, named after the snapcast stream id: snapserver
passes it to the control script as --stream, and the hook takes it as its only
argument (--onevent "/path/to/meta_librespot.py <stream id>").
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import json
import os
import re
import socket
import sys
import tempfile
import time

SOCKET_PREFIX = "snapcast-meta-librespot-"
MAX_DATAGRAM = 65536
LIBRESPOT_MAX_VOLUME = 65535

# Only these are forwarded: the rest of the environment is not ours to copy.
EVENT_VARS = (
    "PLAYER_EVENT",
    "ITEM_TYPE",
    "NAME",
    "URI",
    "DURATION_MS",
    "COVERS",
    "ARTISTS",
    "ALBUM",
    "ALBUM_ARTISTS",
    "NUMBER",
    "DISC_NUMBER",
    "SHOW_NAME",
    "POSITION_MS",
    "VOLUME",
    "SHUFFLE",
    "REPEAT",
    "REPEAT_TRACK",
)


def socket_path(stream: str) -> str:
    """Rendezvous for both roles of one stream: they share snapserver's
    environment, and the stream id keeps several streams apart."""
    # Stream ids are free text ("Air Play", "a/b"); keep the file name plain.
    name = SOCKET_PREFIX + re.sub(r"[^A-Za-z0-9._-]", "_", stream) + ".sock"
    return os.path.join(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir(), name)


# --- Hook role (run by librespot) --------------------------------------
def run_hook(argv: list[str]) -> int:
    event = {k: os.environ[k] for k in EVENT_VARS if k in os.environ}
    if len(argv) != 1:
        # Without the stream id the event has nowhere to go; say so in
        # librespot's log rather than dropping it.
        print(
            f"meta_librespot: event {event['PLAYER_EVENT']} not delivered: the hook needs the snapcast stream id"
            ' as its only argument, e.g. --onevent "/path/to/meta_librespot.py <stream id>"',
            file=sys.stderr,
        )
        return 2
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(json.dumps(event).encode(), socket_path(argv[0]))
    except OSError as e:
        print(f"meta_librespot: event {event['PLAYER_EVENT']} not delivered: {e}", file=sys.stderr)
        return 1
    return 0


# --- Control script role (run by snapserver) ---------------------------
def split_lines(value: str) -> list[str]:
    """librespot joins list values (artists, covers) with newlines."""
    return [v for v in value.split("\n") if v]


def seconds(ms: str) -> float:
    return int(ms) / 1000


def track_metadata(event: dict) -> dict:
    """Snapserver metadata from a track_changed event."""
    md: dict = {
        "title": event["NAME"],
        "trackId": event["URI"],
        "duration": seconds(event["DURATION_MS"]),
    }
    covers = split_lines(event.get("COVERS", ""))
    if covers:
        md["artUrl"] = covers[0]  # librespot lists the widest cover first
    if event["ITEM_TYPE"] == "Episode":
        md["album"] = event["SHOW_NAME"]
    else:
        md["artist"] = split_lines(event["ARTISTS"])
        md["album"] = event["ALBUM"]
        md["albumArtist"] = split_lines(event["ALBUM_ARTISTS"])
        md["trackNumber"] = int(event["NUMBER"])
        md["discNumber"] = int(event["DISC_NUMBER"])
    return md


class Player:
    """Stream properties as known from librespot events. Read-only source:
    librespot has no control API, so every can* flag stays false."""

    def __init__(self) -> None:
        self.properties: dict = {
            "playbackStatus": "stopped",
            "canControl": False,
            "canPlay": False,
            "canPause": False,
            "canGoNext": False,
            "canGoPrevious": False,
            "canSeek": False,
        }
        self.position_at = time.monotonic()

    def set_position(self, position: float) -> None:
        self.properties["position"] = position
        # Stamped so a reader can extrapolate while playing.
        self.position_at = time.monotonic()

    def handle_event(self, event: dict) -> bool:
        """Apply one hook payload; return whether properties changed.
        Raises KeyError/ValueError on a payload missing an expected field."""
        before = copy.deepcopy(self.properties)
        props = self.properties
        name = event["PLAYER_EVENT"]
        # preloading / preload_next fall through on purpose: the preloaded
        # track is not playing yet, track_changed says when it is.
        if name == "track_changed":
            props["metadata"] = track_metadata(event)
            self.set_position(0.0)
        elif name in ("playing", "paused"):
            props["playbackStatus"] = name
            self.set_position(seconds(event["POSITION_MS"]))
        elif name == "stopped":
            props["playbackStatus"] = "stopped"
        elif name in ("seeked", "position_correction"):
            self.set_position(seconds(event["POSITION_MS"]))
        elif name == "volume_changed":
            props["volume"] = round(int(event["VOLUME"]) * 100 / LIBRESPOT_MAX_VOLUME)
        elif name == "shuffle_changed":
            props["shuffle"] = event["SHUFFLE"] == "true"
        elif name == "repeat_changed":
            if event["REPEAT_TRACK"] == "true":
                props["loopStatus"] = "track"
            elif event["REPEAT"] == "true":
                props["loopStatus"] = "playlist"
            else:
                props["loopStatus"] = "none"
        return props != before


def send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def log(severity: str, message: str) -> None:
    send({"jsonrpc": "2.0", "method": "Plugin.Stream.Log", "params": {"severity": severity, "message": message}})


def handle_datagram(player: Player, data: bytes) -> str | None:
    """Apply one hook datagram; return the event name, or None if rejected."""
    try:
        event = json.loads(data)
        changed = player.handle_event(event)
    except (KeyError, TypeError, ValueError) as e:
        log("warning", f"ignoring malformed librespot event ({e!r}): {data[:200]!r}")
        return None
    if changed:
        send({"jsonrpc": "2.0", "method": "Plugin.Stream.Player.Properties", "params": player.properties})
    return str(event["PLAYER_EVENT"])


def handle_request(player: Player, line: bytes) -> None:
    try:
        req = json.loads(line)
        method, req_id = req["method"], req.get("id")
    except (KeyError, TypeError, ValueError) as e:
        log("warning", f"ignoring malformed request ({e!r}): {line[:200]!r}")
        return
    if method == "Plugin.Stream.Player.GetProperties":
        send({"jsonrpc": "2.0", "id": req_id, "result": player.properties})
    elif req_id is not None:
        error = {"code": -32601, "message": f"{method} is not supported: librespot cannot be controlled"}
        send({"jsonrpc": "2.0", "id": req_id, "error": error})


async def run_control(stream: str, mpris: bool) -> int:
    path = socket_path(stream)
    # A previous instance killed by snapserver leaves its socket file behind.
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    events = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    events.bind(path)
    stdin_fd = sys.stdin.fileno()
    loop = asyncio.get_running_loop()
    stdin_closed = loop.create_future()

    player = Player()
    view = None
    pending = b""

    def on_event() -> None:
        name = handle_datagram(player, events.recv(MAX_DATAGRAM))
        if view is not None and name is not None:
            view.sync(name)

    def on_stdin() -> None:
        nonlocal pending
        chunk = os.read(stdin_fd, 4096)
        if not chunk:  # snapserver closed our stdin
            loop.remove_reader(stdin_fd)
            stdin_closed.set_result(None)
            return
        *lines, pending = (pending + chunk).split(b"\n")
        for line in lines:
            if line.strip():
                handle_request(player, line)

    try:
        if mpris:
            # Imported on demand: dbus-fast is only required with --mpris.
            import meta_librespot_mpris

            view = await meta_librespot_mpris.export(player, stream)
        loop.add_reader(events, on_event)
        loop.add_reader(stdin_fd, on_stdin)
        send({"jsonrpc": "2.0", "method": "Plugin.Stream.Ready"})
        await stdin_closed
        return 0
    finally:
        events.close()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # snapserver appends these to every control script; only --stream is used.
    parser.add_argument("--stream", required=True, help="snapcast stream id (passed by snapserver), names the socket")
    parser.add_argument("--snapcast-host", help="snapserver HTTP host (passed by snapserver, unused)")
    parser.add_argument("--snapcast-port", help="snapserver HTTP port (passed by snapserver, unused)")
    parser.add_argument("--mpris", action="store_true", help="also publish a read-only MPRIS player on the session bus")
    return parser.parse_args(argv)


def main() -> int:
    if "PLAYER_EVENT" in os.environ:
        return run_hook(sys.argv[1:])
    args = parse_args(sys.argv[1:])
    return asyncio.run(run_control(args.stream, args.mpris))


if __name__ == "__main__":
    sys.exit(main())
