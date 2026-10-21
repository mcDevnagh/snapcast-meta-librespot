import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import meta_librespot
from meta_librespot import Player, handle_datagram, handle_request, track_metadata

SCRIPT = Path(meta_librespot.__file__)

TRACK_EVENT = {
    "PLAYER_EVENT": "track_changed",
    "ITEM_TYPE": "Track",
    "NAME": "Le tourbillon",
    "URI": "spotify:track:abc",
    "DURATION_MS": "232500",
    "COVERS": "https://i.example/640\nhttps://i.example/300\nhttps://i.example/64",
    "ARTISTS": "Artist A\nArtist B",
    "ALBUM": "Some Album",
    "ALBUM_ARTISTS": "Artist A",
    "NUMBER": "3",
    "DISC_NUMBER": "1",
}


def sent(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


# --- track_metadata ----------------------------------------------------
def test_track_metadata_track():
    assert track_metadata(TRACK_EVENT) == {
        "title": "Le tourbillon",
        "trackId": "spotify:track:abc",
        "duration": 232.5,
        "artUrl": "https://i.example/640",
        "artist": ["Artist A", "Artist B"],
        "album": "Some Album",
        "albumArtist": ["Artist A"],
        "trackNumber": 3,
        "discNumber": 1,
    }


def test_track_metadata_episode_uses_show_as_album():
    md = track_metadata({
        "PLAYER_EVENT": "track_changed",
        "ITEM_TYPE": "Episode",
        "NAME": "Episode 12",
        "URI": "spotify:episode:xyz",
        "DURATION_MS": "1800000",
        "COVERS": "",
        "SHOW_NAME": "Some Show",
    })
    assert md == {
        "title": "Episode 12",
        "trackId": "spotify:episode:xyz",
        "duration": 1800.0,
        "album": "Some Show",
    }


# --- Player ------------------------------------------------------------
def test_player_starts_stopped_and_uncontrollable():
    props = Player().properties
    assert props["playbackStatus"] == "stopped"
    assert not any(v for k, v in props.items() if k.startswith("can"))
    assert "metadata" not in props


def test_track_changed_sets_metadata():
    player = Player()
    assert player.handle_event(TRACK_EVENT) is True
    assert player.properties["metadata"]["title"] == "Le tourbillon"
    assert player.properties["position"] == 0.0


@pytest.mark.parametrize("name", ["preloading", "preload_next", "loading", "end_of_track"])
def test_preload_events_do_not_touch_properties(name):
    # The reason this script exists: a preloaded track is not playing yet.
    player = Player()
    player.handle_event(TRACK_EVENT)
    assert player.handle_event({"PLAYER_EVENT": name, "TRACK_ID": "next"}) is False
    assert player.properties["metadata"]["title"] == "Le tourbillon"


def test_playback_status_and_position():
    player = Player()
    assert player.handle_event({"PLAYER_EVENT": "playing", "POSITION_MS": "1500"}) is True
    assert (player.properties["playbackStatus"], player.properties["position"]) == ("playing", 1.5)
    player.handle_event({"PLAYER_EVENT": "paused", "POSITION_MS": "4000"})
    assert (player.properties["playbackStatus"], player.properties["position"]) == ("paused", 4.0)
    player.handle_event({"PLAYER_EVENT": "seeked", "POSITION_MS": "60000"})
    assert (player.properties["playbackStatus"], player.properties["position"]) == ("paused", 60.0)
    player.handle_event({"PLAYER_EVENT": "stopped"})
    assert player.properties["playbackStatus"] == "stopped"


def test_same_event_twice_reports_no_change():
    player = Player()
    event = {"PLAYER_EVENT": "playing", "POSITION_MS": "0"}
    assert player.handle_event(event) is True
    assert player.handle_event(event) is False


def test_volume_is_scaled_to_percent():
    player = Player()
    player.handle_event({"PLAYER_EVENT": "volume_changed", "VOLUME": "65535"})
    assert player.properties["volume"] == 100
    player.handle_event({"PLAYER_EVENT": "volume_changed", "VOLUME": "32768"})
    assert player.properties["volume"] == 50


@pytest.mark.parametrize(
    ("repeat", "repeat_track", "expected"),
    [("false", "false", "none"), ("true", "false", "playlist"), ("true", "true", "track")],
)
def test_repeat_maps_to_loop_status(repeat, repeat_track, expected):
    player = Player()
    player.handle_event({"PLAYER_EVENT": "repeat_changed", "REPEAT": repeat, "REPEAT_TRACK": repeat_track})
    assert player.properties["loopStatus"] == expected


def test_shuffle():
    player = Player()
    player.handle_event({"PLAYER_EVENT": "shuffle_changed", "SHUFFLE": "true"})
    assert player.properties["shuffle"] is True


def test_missing_field_raises():
    with pytest.raises(KeyError):
        Player().handle_event({"PLAYER_EVENT": "playing"})


# --- stdout protocol ---------------------------------------------------
def test_datagram_emits_properties_only_on_change(capsys):
    player = Player()
    data = json.dumps(TRACK_EVENT).encode()
    handle_datagram(player, data)
    handle_datagram(player, data)
    (msg,) = sent(capsys)
    assert msg["method"] == "Plugin.Stream.Player.Properties"
    assert msg["params"]["metadata"]["artist"] == ["Artist A", "Artist B"]


@pytest.mark.parametrize(
    "data",
    [b"not json", b'{"PLAYER_EVENT": "playing"}', b'{"PLAYER_EVENT": "seeked", "POSITION_MS": "x"}'],
)
def test_malformed_datagram_is_logged_not_fatal(capsys, data):
    handle_datagram(Player(), data)
    (msg,) = sent(capsys)
    assert msg["method"] == "Plugin.Stream.Log"
    assert msg["params"]["severity"] == "warning"


def test_get_properties_request(capsys):
    player = Player()
    handle_request(player, b'{"id": 7, "jsonrpc": "2.0", "method": "Plugin.Stream.Player.GetProperties"}')
    assert sent(capsys) == [{"jsonrpc": "2.0", "id": 7, "result": player.properties}]


def test_control_request_is_refused(capsys):
    req = {"id": 8, "jsonrpc": "2.0", "method": "Plugin.Stream.Player.Control", "params": {"command": "next"}}
    handle_request(Player(), json.dumps(req).encode())
    (msg,) = sent(capsys)
    assert msg["id"] == 8
    assert msg["error"]["code"] == -32601


# --- socket_path -------------------------------------------------------
def test_socket_path_is_per_stream(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert meta_librespot.socket_path("Spotify-HTPC") == str(tmp_path / "snapcast-meta-librespot-Spotify-HTPC.sock")
    assert meta_librespot.socket_path("Spotify-HTPC") != meta_librespot.socket_path("Spotify-Kitchen")


def test_socket_path_keeps_the_file_name_plain(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert Path(meta_librespot.socket_path("Air Play/2")).name == "snapcast-meta-librespot-Air_Play_2.sock"


# --- both roles, as real processes -------------------------------------
def run_hook(runtime_dir: Path, event: dict, *args: str) -> subprocess.CompletedProcess:
    """Run the hook as librespot would; the stream id defaults to SpotifyHD."""
    env = {"PATH": os.environ["PATH"], "XDG_RUNTIME_DIR": str(runtime_dir), **event}
    argv = [sys.executable, str(SCRIPT), *(args or ("SpotifyHD",))]
    return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10)


def start_control(runtime_dir: Path, stream: str = "SpotifyHD") -> subprocess.Popen:
    env = {"PATH": os.environ["PATH"], "XDG_RUNTIME_DIR": str(runtime_dir)}
    args = [sys.executable, str(SCRIPT), f"--stream={stream}", "--snapcast-port=1780", "--snapcast-host=localhost"]
    return subprocess.Popen(args, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)


def test_hook_to_control_script_end_to_end(runtime_dir):
    ctrl = start_control(runtime_dir)
    try:
        assert json.loads(ctrl.stdout.readline()) == {"jsonrpc": "2.0", "method": "Plugin.Stream.Ready"}

        # Preload first, as librespot does ~30 s before the end of a track.
        assert run_hook(runtime_dir, {"PLAYER_EVENT": "preloading", "TRACK_ID": "next"}).returncode == 0
        assert run_hook(runtime_dir, TRACK_EVENT).returncode == 0
        assert run_hook(runtime_dir, {"PLAYER_EVENT": "playing", "POSITION_MS": "0"}).returncode == 0

        first = json.loads(ctrl.stdout.readline())
        assert first["method"] == "Plugin.Stream.Player.Properties"
        assert first["params"]["metadata"]["title"] == "Le tourbillon"
        assert first["params"]["playbackStatus"] == "stopped"
        second = json.loads(ctrl.stdout.readline())
        assert second["params"]["playbackStatus"] == "playing"

        ctrl.stdin.write('{"id": 1, "jsonrpc": "2.0", "method": "Plugin.Stream.Player.GetProperties"}\n')
        ctrl.stdin.flush()
        reply = json.loads(ctrl.stdout.readline())
        assert reply["id"] == 1
        assert reply["result"] == second["params"]

        ctrl.stdin.close()
        assert ctrl.wait(timeout=10) == 0
        assert not (runtime_dir / "snapcast-meta-librespot-SpotifyHD.sock").exists()
    finally:
        ctrl.kill()
        ctrl.wait()


def test_two_streams_get_their_own_events(runtime_dir):
    # The reason for per-stream sockets: one snapserver, several librespots.
    ctrls = {stream: start_control(runtime_dir, stream) for stream in ("Spotify-HTPC", "Spotify-Kitchen")}
    try:
        for ctrl in ctrls.values():
            assert json.loads(ctrl.stdout.readline())["method"] == "Plugin.Stream.Ready"

        assert run_hook(runtime_dir, TRACK_EVENT, "Spotify-HTPC").returncode == 0
        assert run_hook(runtime_dir, {"PLAYER_EVENT": "playing", "POSITION_MS": "0"}, "Spotify-Kitchen").returncode == 0

        htpc = json.loads(ctrls["Spotify-HTPC"].stdout.readline())["params"]
        kitchen = json.loads(ctrls["Spotify-Kitchen"].stdout.readline())["params"]
        assert (htpc["metadata"]["title"], htpc["playbackStatus"]) == ("Le tourbillon", "stopped")
        assert ("metadata" not in kitchen, kitchen["playbackStatus"]) == (True, "playing")
    finally:
        for ctrl in ctrls.values():
            ctrl.kill()
            ctrl.wait()


def test_hook_without_control_script_fails_with_a_message(runtime_dir):
    result = run_hook(runtime_dir, {"PLAYER_EVENT": "playing", "POSITION_MS": "0"})
    assert result.returncode == 1
    assert "playing not delivered" in result.stderr


def test_hook_without_stream_id_fails_with_a_message(runtime_dir):
    env = {"PATH": os.environ["PATH"], "XDG_RUNTIME_DIR": str(runtime_dir), "PLAYER_EVENT": "playing"}
    result = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "playing not delivered" in result.stderr
    assert "stream id" in result.stderr


def test_control_script_requires_stream(runtime_dir):
    env = {"PATH": os.environ["PATH"], "XDG_RUNTIME_DIR": str(runtime_dir)}
    result = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "--stream" in result.stderr
