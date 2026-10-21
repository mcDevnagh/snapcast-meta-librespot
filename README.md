# snapcast-meta-librespot

Snapcast control script that gives a [librespot](https://github.com/librespot-org/librespot)
stream full metadata (title, artists, album, cover, duration) and playback
status, taken from librespot's `--onevent` hook.

## Why

Snapserver's built-in `librespot://` source reads the title from librespot's
log line `<Title> (N ms) loaded`. librespot also prints that line when it
preloads the next track, about 30 seconds before the current one ends, so the
title changes early, and artist and album are never available.

This script uses the `track_changed` event instead, which librespot emits when
a track starts playing. `preloading` and `preload_next` are ignored.

## How it works

`meta_librespot.py` has two roles, told apart by the `PLAYER_EVENT`
environment variable:

- **hook**: librespot runs it once per player event (`--onevent`). It forwards
  the event to the control script over a Unix datagram socket and exits.
- **control script**: snapserver runs it (`controlscript=`). It listens on the
  socket and publishes stream properties to snapserver on stdout.

Each stream has its own socket,
`$XDG_RUNTIME_DIR/snapcast-meta-librespot-<stream id>.sock` (in the system
temporary directory when `XDG_RUNTIME_DIR` is unset), so one snapserver can run
several librespot streams. Characters other than letters, digits, `.`, `_` and
`-` in the stream id become `_`. The control script takes the stream id from
the `--stream` argument snapserver always passes; the hook takes it as its only
argument (see Installation). Both roles are started under snapserver, so they
resolve the same path.

## Requirements

- Python 3.11 or later, standard library only.
- [dbus-fast](https://github.com/Bluetooth-Devices/dbus-fast) (Debian:
  `python3-dbus-fast`), only with `--mpris`.
- A librespot whose `track_changed` event carries `NAME`, `ARTISTS`, `ALBUM`,
  `COVERS` and `DURATION_MS`. To check, point `--onevent` at a script that runs
  `env` and look for those variables.

## Installation

Copy `meta_librespot.py` to snapserver's plugin directory (`plugin_dir` in the
`[stream]` section of `snapserver.conf`, `/usr/share/snapserver/plug-ins` by
default) and keep it executable. Copy `meta_librespot_mpris.py` next to it if
you use `--mpris`.

Declare the stream as a `process://` source. The `librespot://` source type
must not be used: its log parser would keep overwriting the metadata.

```ini
source = process:///usr/bin/librespot?name=Spotify&sampleformat=44100:16:2&wd_timeout=7800&params=--name%20Snapcast%20--bitrate%20320%20--backend%20pipe%20--initial-volume%20100%20--verbose%20--onevent%20%22/usr/share/snapserver/plug-ins/meta_librespot.py%20Spotify%22&controlscript=meta_librespot.py
```

- `params` holds the librespot command line, URL-encoded (`%20` for a space,
  `%22` for a double quote). `--backend pipe` is required.
- `--onevent` takes the absolute path of the script followed by the stream id,
  in double quotes so that snapserver hands both to librespot as one argument;
  librespot splits it again when it runs the hook. The id must match the
  source's `name=` exactly (`Spotify` above), or the stream gets no metadata.
  A hook run without an id says so in librespot's log and exits with status 2.
  The script path must not contain spaces.
- For more streams, repeat the line with another `name=`, librespot `--name`
  and `--zeroconf-port` (if set), and the matching id after the script path.
- `wd_timeout=7800` and `--verbose` reproduce the `librespot://` behaviour
  (restart librespot after 130 minutes without log output). Both are optional.
- Add `log_stderr=true` to get librespot's log lines in snapserver's log.

## MPRIS

With `controlscriptparams=--mpris` appended to the source line, the control
script also publishes a read-only MPRIS player on the session bus. It shows
what plays on the server host (status, metadata, position, volume) to any
MPRIS client.

Each stream is its own MPRIS instance, as the MPRIS spec suggests for several
instances of one player:

- Bus name `org.mpris.MediaPlayer2.librespot.<stream id>`, e.g.
  `org.mpris.MediaPlayer2.librespot.Spotify_HTPC`. Characters other than
  letters, digits and `_` become `_`, and an id starting with a digit gets a
  leading `_`.
- `Identity` is `librespot (<stream id>)`, so media widgets can tell the
  streams apart.
- `playerctl -p librespot` matches every instance;
  `playerctl -p librespot.Spotify_HTPC` picks one.

Notes:

- Session bus only: snapserver must run in a user session, so that the script
  inherits `DBUS_SESSION_BUS_ADDRESS`.
- No control: every `Can*` property is false and every method is refused.
- If the bus is unreachable, dbus-fast is missing or the name is already
  owned (the same stream id twice on one bus), the script exits at startup
  and the stream gets no metadata at all.

## Limitations

- Read-only: librespot has no control API, so play, pause, next and previous
  are not available (`canControl` is false).
- Metadata still leads the audio by snapserver's stream buffer (1 second by
  default), like any other source.

## Development

```sh
uv sync
uv run pytest -q
uv run ruff check .
uv run mypy
```

The MPRIS tests need `dbus-daemon` and are skipped without it.

## License

GNU General Public License, version 3 or (at your option) any later version
(`GPL-3.0-or-later`), like the plugins shipped with Snapcast. See
[LICENSE](LICENSE).
