import asyncio
import json
import os
import shutil
import subprocess
import sys

import pytest

pytest.importorskip("dbus_fast")

from dbus_fast import BusType, Message, MessageType  # noqa: E402
from dbus_fast.aio import MessageBus  # noqa: E402
from test_meta_librespot import SCRIPT, TRACK_EVENT, run_hook  # noqa: E402

import meta_librespot_mpris  # noqa: E402
from meta_librespot import Player  # noqa: E402
from meta_librespot_mpris import (  # noqa: E402
    NO_TRACK,
    ROOT_PATH,
    PlayerInterface,
    bus_name,
    mpris_metadata,
    track_path,
)

PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"


def plain(metadata: dict) -> dict:
    return {k: (v.signature, v.value) for k, v in metadata.items()}


# --- metadata mapping --------------------------------------------------
def test_no_track_metadata():
    assert plain(mpris_metadata(None)) == {"mpris:trackid": ("o", NO_TRACK)}


def test_track_metadata_mapping():
    player = Player()
    player.handle_event(TRACK_EVENT)
    assert plain(mpris_metadata(player.properties["metadata"])) == {
        "mpris:trackid": ("o", "/org/mpris/MediaPlayer2/Track/spotify_track_abc"),
        "mpris:length": ("x", 232_500_000),
        "mpris:artUrl": ("s", "https://i.example/640"),
        "xesam:title": ("s", "Le tourbillon"),
        "xesam:url": ("s", "spotify:track:abc"),
        "xesam:artist": ("as", ["Artist A", "Artist B"]),
        "xesam:album": ("s", "Some Album"),
        "xesam:albumArtist": ("as", ["Artist A"]),
        "xesam:trackNumber": ("i", 3),
        "xesam:discNumber": ("i", 1),
    }


def test_track_path_is_a_valid_object_path():
    assert track_path("spotify:episode:a-b.c") == "/org/mpris/MediaPlayer2/Track/spotify_episode_a_b_c"


# --- bus name ----------------------------------------------------------
@pytest.mark.parametrize(
    ("stream", "expected"),
    [
        ("SpotifyHD", "org.mpris.MediaPlayer2.librespot.SpotifyHD"),
        ("Spotify-HTPC", "org.mpris.MediaPlayer2.librespot.Spotify_HTPC"),
        ("Air Play/2", "org.mpris.MediaPlayer2.librespot.Air_Play_2"),
        # A bus name element must not start with a digit, nor be empty.
        ("2nd", "org.mpris.MediaPlayer2.librespot._2nd"),
        ("", "org.mpris.MediaPlayer2.librespot._"),
    ],
)
def test_bus_name_is_one_instance_per_stream(stream, expected):
    assert bus_name(stream) == expected


# --- PlayerInterface ---------------------------------------------------
@pytest.fixture
def view(monkeypatch):
    player = Player()
    view = PlayerInterface(player)
    view.changes = []
    view.seeks = []
    monkeypatch.setattr(view, "emit_properties_changed", view.changes.append)
    monkeypatch.setattr(view, "Seeked", view.seeks.append)
    view.player = player
    return view


def feed(view, event: dict) -> None:
    view.player.handle_event(event)
    view.sync(event["PLAYER_EVENT"])


def test_sync_emits_only_what_changed(view):
    feed(view, TRACK_EVENT)
    feed(view, {"PLAYER_EVENT": "playing", "POSITION_MS": "0"})
    feed(view, {"PLAYER_EVENT": "volume_changed", "VOLUME": "32768"})
    assert [sorted(c) for c in view.changes] == [["Metadata"], ["PlaybackStatus"], ["Volume"]]
    assert view.changes[1] == {"PlaybackStatus": "Playing"}
    assert view.changes[2] == {"Volume": 0.5}


def test_preload_emits_nothing(view):
    feed(view, TRACK_EVENT)
    view.changes.clear()
    feed(view, {"PLAYER_EVENT": "preloading", "TRACK_ID": "next"})
    assert view.changes == []


def test_seek_emits_seeked_not_properties_changed(view):
    feed(view, {"PLAYER_EVENT": "paused", "POSITION_MS": "1000"})
    view.changes.clear()
    feed(view, {"PLAYER_EVENT": "seeked", "POSITION_MS": "60000"})
    assert view.changes == []
    assert view.seeks == [60_000_000]


def test_position_runs_only_while_playing(view, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(meta_librespot_mpris.time, "monotonic", lambda: now[0])
    feed(view, TRACK_EVENT)
    feed(view, {"PLAYER_EVENT": "playing", "POSITION_MS": "10000"})
    now[0] += 5
    assert view._position_us() == 15_000_000
    feed(view, {"PLAYER_EVENT": "paused", "POSITION_MS": "15000"})
    now[0] += 60
    assert view._position_us() == 15_000_000


def test_position_is_capped_at_track_duration(view, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(meta_librespot_mpris.time, "monotonic", lambda: now[0])
    feed(view, TRACK_EVENT)
    feed(view, {"PLAYER_EVENT": "playing", "POSITION_MS": "230000"})
    now[0] += 3600
    assert view._position_us() == 232_500_000


# --- on a private session bus, as real processes -----------------------
@pytest.fixture
def bus_address():
    if shutil.which("dbus-daemon") is None:
        pytest.skip("dbus-daemon not available")
    daemon = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address"], stdout=subprocess.PIPE, text=True
    )
    try:
        yield daemon.stdout.readline().strip()
    finally:
        daemon.kill()
        daemon.wait()


async def call(
    bus: MessageBus,
    interface: str,
    member: str,
    signature: str = "",
    body: list | None = None,
    stream: str = "SpotifyHD",
) -> Message:
    message = Message(
        destination=bus_name(stream),
        path=ROOT_PATH,
        interface=interface,
        member=member,
        signature=signature,
        body=body or [],
    )
    return await bus.call(message)


def start_control(env: dict, stream: str = "SpotifyHD") -> subprocess.Popen:
    args = [sys.executable, str(SCRIPT), f"--stream={stream}", "--mpris"]
    pipe = subprocess.PIPE
    return subprocess.Popen(args, env=env, stdin=pipe, stdout=pipe, stderr=pipe, text=True)


def test_mpris_end_to_end(runtime_dir, bus_address):
    env = {"PATH": os.environ["PATH"], "XDG_RUNTIME_DIR": str(runtime_dir), "DBUS_SESSION_BUS_ADDRESS": bus_address}
    ctrl = start_control(env)
    try:
        assert json.loads(ctrl.stdout.readline())["method"] == "Plugin.Stream.Ready"
        assert run_hook(runtime_dir, {"PLAYER_EVENT": "preloading", "TRACK_ID": "next"}).returncode == 0
        assert run_hook(runtime_dir, TRACK_EVENT).returncode == 0
        assert run_hook(runtime_dir, {"PLAYER_EVENT": "playing", "POSITION_MS": "42000"}).returncode == 0
        # Both notifications to snapserver are out, so MPRIS is in sync too.
        ctrl.stdout.readline()
        ctrl.stdout.readline()

        async def read_player() -> tuple[dict, Message, Message]:
            bus = await MessageBus(bus_address=bus_address, bus_type=BusType.SESSION).connect()
            get_all = "org.freedesktop.DBus.Properties", "GetAll", "s"
            props = (await call(bus, *get_all, [PLAYER_IFACE])).body[0]
            root = await call(bus, *get_all, ["org.mpris.MediaPlayer2"])
            refused = await call(bus, PLAYER_IFACE, "Next")
            bus.disconnect()
            return props, root, refused

        props, root, refused = asyncio.run(read_player())
        assert props["PlaybackStatus"].value == "Playing"
        assert props["Metadata"].value["xesam:title"].value == "Le tourbillon"
        assert props["Metadata"].value["xesam:artist"].value == ["Artist A", "Artist B"]
        assert 42_000_000 <= props["Position"].value < 52_000_000
        assert props["CanControl"].value is False
        assert root.body[0]["Identity"].value == "librespot (SpotifyHD)"
        assert refused.message_type == MessageType.ERROR
        assert refused.error_name == "org.freedesktop.DBus.Error.NotSupported"

        ctrl.stdin.close()
        assert ctrl.wait(timeout=10) == 0
    finally:
        ctrl.kill()
        ctrl.wait()


def test_mpris_one_instance_per_stream(runtime_dir, bus_address):
    # Two streams on one session bus: each owns its own name and only shows
    # its own events.
    env = {"PATH": os.environ["PATH"], "XDG_RUNTIME_DIR": str(runtime_dir), "DBUS_SESSION_BUS_ADDRESS": bus_address}
    ctrls = {stream: start_control(env, stream) for stream in ("Spotify-HTPC", "Spotify-Kitchen")}
    try:
        for ctrl in ctrls.values():
            assert json.loads(ctrl.stdout.readline())["method"] == "Plugin.Stream.Ready"
        assert run_hook(runtime_dir, TRACK_EVENT, "Spotify-HTPC").returncode == 0
        assert run_hook(runtime_dir, {"PLAYER_EVENT": "playing", "POSITION_MS": "0"}, "Spotify-Kitchen").returncode == 0
        for ctrl in ctrls.values():
            ctrl.stdout.readline()

        async def read_players() -> dict[str, tuple[dict, dict]]:
            bus = await MessageBus(bus_address=bus_address, bus_type=BusType.SESSION).connect()
            get_all = "org.freedesktop.DBus.Properties", "GetAll", "s"
            seen = {}
            for stream in ctrls:
                props = (await call(bus, *get_all, [PLAYER_IFACE], stream=stream)).body[0]
                root = (await call(bus, *get_all, ["org.mpris.MediaPlayer2"], stream=stream)).body[0]
                seen[stream] = props, root
            bus.disconnect()
            return seen

        seen = asyncio.run(read_players())
        htpc, htpc_root = seen["Spotify-HTPC"]
        kitchen, kitchen_root = seen["Spotify-Kitchen"]
        assert (htpc["PlaybackStatus"].value, htpc["Metadata"].value["xesam:title"].value) == (
            "Stopped",
            "Le tourbillon",
        )
        assert (kitchen["PlaybackStatus"].value, "xesam:title" in kitchen["Metadata"].value) == ("Playing", False)
        assert htpc_root["Identity"].value == "librespot (Spotify-HTPC)"
        assert kitchen_root["Identity"].value == "librespot (Spotify-Kitchen)"
    finally:
        for ctrl in ctrls.values():
            ctrl.kill()
            ctrl.wait()


def test_mpris_name_already_owned_fails_before_ready(runtime_dir, bus_address):
    # Only the same stream twice on one bus collides now.
    env = {"PATH": os.environ["PATH"], "XDG_RUNTIME_DIR": str(runtime_dir), "DBUS_SESSION_BUS_ADDRESS": bus_address}
    first = start_control(env)
    try:
        assert json.loads(first.stdout.readline())["method"] == "Plugin.Stream.Ready"
        second_env = {**env, "XDG_RUNTIME_DIR": str(runtime_dir / "second")}
        (runtime_dir / "second").mkdir()
        second = start_control(second_env)
        out, err = second.communicate(timeout=10)
        assert second.returncode != 0
        assert out == ""
        assert "already owned" in err
    finally:
        first.kill()
        first.wait()


def test_mpris_without_session_bus_fails_before_ready(runtime_dir):
    env = {"PATH": os.environ["PATH"], "XDG_RUNTIME_DIR": str(runtime_dir)}
    ctrl = start_control(env)
    out, err = ctrl.communicate(timeout=10)
    assert ctrl.returncode != 0
    assert out == ""
