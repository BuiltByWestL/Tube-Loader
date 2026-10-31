# Tube Loader

A small, fast, local YouTube downloader. A black-and-white front-end for [yt-dlp](https://github.com/yt-dlp/yt-dlp)
that runs on your machine, needs no installs beyond Python 3, and keeps its own tools up to date.

## Run

Double-click **`TubeLoader.pyw`** (or `Tube Loader.lnk`, which has the icon, once you've created it - see below).
It opens in its own Edge/Chrome app window and quits when you close the window and nothing is downloading.

On first launch it downloads, from GitHub, into `bin/`:

- `yt-dlp.exe` - checked against the release's SHA-256 sums; re-checked for a newer release on every launch
- `ffmpeg` / `ffprobe` - only if they aren't already on your PATH
- Deno - only if neither Deno nor Node is available (YouTube extraction needs a JavaScript runtime)

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
- `ui/index.html` - the whole interface
- `data/config.json` - your folders and remembered artists (not committed)
- `bin/` - downloaded tools (not committed)

To make a shortcut with the icon: right-click `TubeLoader.pyw` -> Create shortcut, then set its icon to `icon.ico`.

## Security

The server listens on `127.0.0.1` only, rejects unexpected `Host` headers, and requires a random per-launch token on every API call,
so other websites can't drive it.
