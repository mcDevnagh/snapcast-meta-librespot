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

"""MPRIS2 view of the librespot stream, enabled by ``meta_librespot.py --mpris``.

Read-only: librespot cannot be controlled from here, so every Can* flag is
false and every method is refused.

Each stream is its own MPRIS instance, as the MPRIS spec suggests for several
instances of one player: the bus name ends with the snapcast stream id
(org.mpris.MediaPlayer2.librespot.<stream id>), and Identity names the stream.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from dbus_fast import NameFlag, RequestNameReply, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.errors import DBusError
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method, signal

if TYPE_CHECKING:
    from meta_librespot import Player

ROOT_PATH = "/org/mpris/MediaPlayer2"
ROOT_IFACE = "org.mpris.MediaPlayer2"
PLAYER_NAME = "librespot"
IDENTITY = "librespot"
NO_TRACK = f"{ROOT_PATH}/TrackList/NoTrack"
NOT_SUPPORTED = "org.freedesktop.DBus.Error.NotSupported"

# snapserver metadata key -> (MPRIS key, D-Bus signature)
OPTIONAL_METADATA = {
    "artUrl": ("mpris:artUrl", "s"),
    "artist": ("xesam:artist", "as"),
    "album": ("xesam:album", "s"),
    "albumArtist": ("xesam:albumArtist", "as"),
    "trackNumber": ("xesam:trackNumber", "i"),
    "discNumber": ("xesam:discNumber", "i"),
}


def refuse(name: str) -> DBusError:
    return DBusError(NOT_SUPPORTED, f"{name} is not supported: librespot cannot be controlled")


def bus_name(stream: str) -> str:
    """Well-known bus name for one stream's MPRIS instance. A bus name element
    allows [A-Za-z0-9_-] and must not start with a digit; '-' is avoided too,
    as the D-Bus spec discourages it."""
    instance = re.sub(r"[^A-Za-z0-9_]", "_", stream)
    if not instance or instance[0].isdigit():
        instance = "_" + instance
    return f"{ROOT_IFACE}.{PLAYER_NAME}.{instance}"


def track_path(uri: str) -> str:
    """D-Bus object path for a Spotify URI (path elements allow [A-Za-z0-9_] only)."""
    return f"{ROOT_PATH}/Track/" + re.sub(r"[^A-Za-z0-9_]", "_", uri)


def mpris_metadata(md: dict | None) -> dict:
    """MPRIS Metadata from the snapserver metadata built by meta_librespot."""
    if not md:
        return {"mpris:trackid": Variant("o", NO_TRACK)}
    out = {
        "mpris:trackid": Variant("o", track_path(md["trackId"])),
        "mpris:length": Variant("x", round(md["duration"] * 1_000_000)),
        "xesam:title": Variant("s", md["title"]),
        "xesam:url": Variant("s", md["trackId"]),
    }
    for key, (name, signature) in OPTIONAL_METADATA.items():
        if key in md:
            out[name] = Variant(signature, md[key])
    return out


class Root(ServiceInterface):
    def __init__(self, stream: str) -> None:
        super().__init__(ROOT_IFACE)
        self._identity = f"{IDENTITY} ({stream})"

    @method()
    def Raise(self):
        raise refuse("Raise")

    @method()
    def Quit(self):
        raise refuse("Quit")

    @dbus_property(access=PropertyAccess.READ)
    def CanQuit(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanRaise(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def HasTrackList(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def Identity(self) -> "s":
        return self._identity

    @dbus_property(access=PropertyAccess.READ)
    def SupportedUriSchemes(self) -> "as":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def SupportedMimeTypes(self) -> "as":
        return []


class PlayerInterface(ServiceInterface):
    """org.mpris.MediaPlayer2.Player backed by a meta_librespot Player."""

    def __init__(self, player: Player) -> None:
        super().__init__(f"{ROOT_IFACE}.Player")
        self._player = player
        self._published = self._state()

    def _state(self) -> dict:
        props = self._player.properties
        return {
            "PlaybackStatus": props["playbackStatus"].capitalize(),
            "Metadata": mpris_metadata(props.get("metadata")),
            "Volume": props.get("volume", 100) / 100,
        }

    def _position_us(self) -> int:
        """Position in microseconds, extrapolated while playing."""
        props = self._player.properties
        position: float = props.get("position", 0.0)
        if props["playbackStatus"] == "playing":
            position += time.monotonic() - self._player.position_at
            duration = props.get("metadata", {}).get("duration")
            if duration is not None:
                position = min(position, duration)
        return round(position * 1_000_000)

    def sync(self, event: str) -> None:
        """Publish what the librespot event ``event`` changed."""
        state = self._state()
        changed = {k: v for k, v in state.items() if v != self._published[k]}
        self._published = state
        if changed:
            self.emit_properties_changed(changed)
        if event in ("seeked", "position_correction"):
            self.Seeked(self._position_us())

    # --- signals -------------------------------------------------------
    @signal()
    def Seeked(self, position: int) -> "x":
        return position

    # --- methods (all refused) -----------------------------------------
    @method()
    def Next(self):
        raise refuse("Next")

    @method()
    def Previous(self):
        raise refuse("Previous")

    @method()
    def Pause(self):
        raise refuse("Pause")

    @method()
    def PlayPause(self):
        raise refuse("PlayPause")

    @method()
    def Stop(self):
        raise refuse("Stop")

    @method()
    def Play(self):
        raise refuse("Play")

    @method()
    def Seek(self, Offset: "x"):
        raise refuse("Seek")

    @method()
    def SetPosition(self, TrackId: "o", Position: "x"):
        raise refuse("SetPosition")

    @method()
    def OpenUri(self, Uri: "s"):
        raise refuse("OpenUri")

    # --- properties ----------------------------------------------------
    @dbus_property(access=PropertyAccess.READ)
    def PlaybackStatus(self) -> "s":
        return self._state()["PlaybackStatus"]

    @dbus_property(access=PropertyAccess.READ)
    def Metadata(self) -> "a{sv}":
        return self._state()["Metadata"]

    @dbus_property(access=PropertyAccess.READ)
    def Volume(self) -> "d":
        return self._state()["Volume"]

    @dbus_property(access=PropertyAccess.READ)
    def Position(self) -> "x":
        return self._position_us()

    @dbus_property(access=PropertyAccess.READ)
    def Rate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def MinimumRate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def MaximumRate(self) -> "d":
        return 1.0

    @dbus_property(access=PropertyAccess.READ)
    def CanGoNext(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanGoPrevious(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanPlay(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanPause(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanSeek(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def CanControl(self) -> "b":
        return False


async def export(player: Player, stream: str) -> PlayerInterface:
    """Publish ``player`` on the session bus as the MPRIS instance of
    ``stream``. Raises if the bus is unreachable or the name is already
    owned."""
    bus = await MessageBus().connect()
    view = PlayerInterface(player)
    bus.export(ROOT_PATH, Root(stream))
    bus.export(ROOT_PATH, view)
    name = bus_name(stream)
    reply = await bus.request_name(name, NameFlag.DO_NOT_QUEUE)
    if reply != RequestNameReply.PRIMARY_OWNER:
        raise RuntimeError(f"D-Bus name {name} is already owned by another process")
    return view
