# Tube Loader

A small, fast, local YouTube downloader. A black-and-white front-end for [yt-dlp](https://github.com/yt-dlp/yt-dlp)
that runs on your machine as a single `.exe` with nothing to install, and keeps its own tools up to date.

## Run

**`TubeLoader.exe`** is one self-contained file: no Python, yt-dlp or ffmpeg install needed. Put it anywhere and double-click it.
It opens in its own Edge/Chrome app window and quits when you close the window and nothing is downloading.

On first launch it sets itself up in `%LOCALAPPDATA%\TubeLoader\bin`, installing **only what's missing**:

| Tool | If already on this PC | Otherwise |
|---|---|---|
| yt-dlp | used as-is (updated with `yt-dlp -U`) | downloaded from GitHub, checked against the release's SHA-256 sums, re-checked for updates every launch |
| ffmpeg / ffprobe | used as-is | downloaded (~330 MB unpacked, one time) |
| JavaScript runtime (YouTube needs one) | Deno or Node used as-is | Deno downloaded |

Settings and remembered artists live in `%LOCALAPPDATA%\TubeLoader\data`.

### Build the exe

`build.bat` -> `dist\TubeLoader.exe` (needs Python 3.9+ and internet on the build machine only; installs PyInstaller).
You can also run from source with `python TubeLoader.pyw`; it then keeps `bin/` and `data/` next to the script.

## Modes

| Mode | yt-dlp arguments |
|---|---|
| **Song** | `-f bestaudio -x --audio-format mp3 --embed-thumbnail` |
| **Video** | `-S res,ext:mp4:m4a --recode-video mp4` |
| **Custom** | audio (mp3, m4a, opus, ogg, flac, wav, aac, original) or video (mp4/mkv/webm, max resolution, video/audio codec preference, optional re-encode) |

"Embed thumbnail" and "Embed metadata" are switches on every mode. The exact command is always shown (and copyable).

## Artist folders

Songs are filed into `<Music folder>\<Artist>\`.

1. The artist is detected from the video (YouTube Music metadata, then an "Artist - Title" title, then the channel) and is editable.
2. If a folder for that artist already exists (any capitalisation/punctuation), it's reused; otherwise it's created.
3. To send an artist somewhere specific, change the **Folder** field (e.g. `aath`) and leave "Remember" on. Future downloads by that artist go there automatically.
   Manage the list under Settings -> Artist folders.

Playlists are expanded into one download per item; the artist is detected per song unless you type one.

## Files

- `TubeLoader.pyw` - server, queue, updater (standard library only)
- `build.bat` - builds the single-file exe
- `ui/index.html` - the whole interface

## Security

The server listens on `127.0.0.1` only, rejects unexpected `Host` headers, and requires a random per-launch token on every API call,
so other websites can't drive it.
