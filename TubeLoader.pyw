"""Tube Loader - a tiny local front-end for yt-dlp. Standard library only.

Double-click to run (no console). Starts a loopback-only HTTP server, opens the UI in
an Edge/Chrome "app" window, keeps yt-dlp / ffmpeg up to date from GitHub, and exits
when the window is closed and nothing is downloading.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP = Path(__file__).resolve().parent
UI_DIR, BIN, DATA = APP / "ui", APP / "bin", APP / "data"
YTDLP = BIN / "yt-dlp.exe"
CONFIG_FILE = DATA / "config.json"
INSTANCE_FILE = DATA / "instance.json"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
UA = "TubeLoader/1.0"
TOKEN = secrets.token_urlsafe(24)
ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

FFMPEG_ZIP = "https://github.com/yt-dlp/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"
DENO_ZIP = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"

# ---------------------------------------------------------------- config

DEFAULTS = {
    "music_dir": str(Path.home() / "Music"),
    "video_dir": str(Path.home() / "Videos"),
    "auto_update": True,
    "artists": {},  # "Ax and the Hatchetmen" -> "aath"
    "ui": {},
}
_cfg_lock = threading.Lock()
_cfg: dict = {}


def load_cfg() -> None:
    global _cfg
    try:
        saved = json.loads(CONFIG_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        saved = {}
    _cfg = {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def save_cfg() -> None:
    DATA.mkdir(exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_cfg, indent=2), "utf-8")
    os.replace(tmp, CONFIG_FILE)


def cfg_snapshot() -> dict:
    with _cfg_lock:
        return json.loads(json.dumps(_cfg))


def update_cfg(patch: dict) -> dict:
    with _cfg_lock:
        for key in ("music_dir", "video_dir"):
            v = patch.get(key)
            if isinstance(v, str) and v.strip() and Path(v).is_absolute():
                _cfg[key] = str(Path(v))
        if isinstance(patch.get("auto_update"), bool):
            _cfg["auto_update"] = patch["auto_update"]
        if isinstance(patch.get("artists"), dict):
            _cfg["artists"] = {str(k).strip(): str(v).strip() for k, v in patch["artists"].items()
                               if str(k).strip() and str(v).strip()}
        if isinstance(patch.get("ui"), dict) and len(json.dumps(patch["ui"])) < 20000:
            _cfg["ui"] = patch["ui"]
        save_cfg()
    return cfg_snapshot()


# ---------------------------------------------------------------- naming helpers

_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", s.casefold())


def safe_name(s: str, fallback: str = "") -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s or "").strip().rstrip(". ")
    if s.upper().split(".")[0] in _RESERVED:
        s = "_" + s
    return s[:120] or fallback


def safe_rel(folder: str) -> Path:
    """User-typed folder -> safe relative path (nested folders allowed, no traversal)."""
    parts = [safe_name(p) for p in re.split(r"[\\/]+", folder)]
    return Path(*[p for p in parts if p]) if any(parts) else Path("Unknown")


def resolve_folder(parent: Path, artist: str) -> tuple[str, str]:
    """Remembered alias -> existing folder (any casing/punctuation) -> new folder named after the artist."""
    n = norm(artist)
    with _cfg_lock:
        aliases = dict(_cfg["artists"])
    for name, folder in aliases.items():
        if norm(name) == n:
            return folder, "alias"
    try:
        for d in parent.iterdir():
            if d.is_dir() and norm(d.name) == n:
                return d.name, "existing"
    except OSError:
        pass
    return safe_name(artist, "Unknown"), "new"


def list_folders(parent: Path, limit: int = 3000) -> list[str]:
    try:
        return sorted((d.name for d in parent.iterdir() if d.is_dir()), key=str.casefold)[:limit]
    except OSError:
        return []


def guess_artist(info: dict) -> str:
    artists = info.get("artists")
    a = (artists[0] if isinstance(artists, list) and artists else None) or info.get("artist") or info.get("creator")
    if not a:
        m = re.match(r"^\s*(.+?)\s+[-–—]\s+\S", info.get("title") or "")
        a = m.group(1) if m else (info.get("channel") or info.get("uploader") or "Unknown")
    a = re.split(r",|\s+(?:feat\.?|ft\.?|featuring)\s+", a, maxsplit=1, flags=re.I)[0]
    a = re.sub(r"\s*(?:-\s*Topic|VEVO)\s*$", "", a, flags=re.I)
    return a.strip() or "Unknown"


def clean_stem(title: str, artist: str, track: str | None) -> str:
    s = track or title or "download"
    if not track:
        m = re.match(r"^\s*(.+?)\s*[-–—:]\s+(\S.*?)\s*$", s)
        if m and norm(m.group(1)) == norm(artist):
            s = m.group(2)
    return safe_name(s, "download")


# ---------------------------------------------------------------- yt-dlp argument building

CHOICES = {
    "kind": ("audio", "video"),
    "afmt": ("mp3", "m4a", "opus", "vorbis", "flac", "wav", "aac", "best"),
    "aq": ("best", "320K", "256K", "192K", "128K"),
    "res": ("best", "2160", "1440", "1080", "720", "480", "360"),
    "container": ("mp4", "mkv", "webm"),
    "vcodec": ("any", "h264", "vp9", "av1"),
    "acodec": ("any", "aac", "opus"),
}
LOSSY = {"mp3", "m4a", "aac", "opus", "vorbis"}
NO_COVER = {"wav", "aac"}


def clean_opts(raw: dict) -> dict:
    o = {k: (raw.get(k) if raw.get(k) in vals else vals[0]) for k, vals in CHOICES.items()}
    for k in ("thumb", "meta", "recode", "sort_artist"):
        o[k] = bool(raw.get(k))
    o["mode"] = raw.get("mode") if raw.get("mode") in ("song", "video", "custom") else "song"
    if o["mode"] == "song":
        o.update(kind="audio", sort_artist=True)
    elif o["mode"] == "video":
        o.update(kind="video", sort_artist=False)
    return o


def build_args(o: dict) -> list[str]:
    """The format-related part of the yt-dlp command line (what the UI shows and runs)."""
    if o["mode"] == "song":
        a = ["-f", "bestaudio", "-x", "--audio-format", "mp3"]
        return a + (["--embed-thumbnail"] if o["thumb"] else [])
    if o["mode"] == "video":
        a = ["-S", "res,ext:mp4:m4a", "--recode-video", "mp4"]
        return a + (["--embed-thumbnail"] if o["thumb"] else [])
    a: list[str] = []
    if o["kind"] == "audio":
        a += ["-f", "bestaudio/best", "-x", "--audio-format", o["afmt"]]
        if o["afmt"] in LOSSY and o["aq"] != "best":
            a += ["--audio-quality", o["aq"]]
        if o["thumb"] and o["afmt"] not in NO_COVER:
            a.append("--embed-thumbnail")
    else:
        sort = ["res" if o["res"] == "best" else f"res:{o['res']}"]
        if o["vcodec"] != "any":
            sort.append(f"vcodec:{o['vcodec']}")
        if o["acodec"] != "any":
            sort.append(f"acodec:{o['acodec']}")
        if o["container"] == "mp4":
            sort.append("ext:mp4:m4a")
        a += ["-S", ",".join(sort), "--merge-output-format", o["container"]]
        if o["recode"]:
            a += ["--recode-video", o["container"]]
        if o["thumb"]:
            a.append("--embed-thumbnail")
    if o["meta"]:
        a.append("--embed-metadata")
    return a


def describe(o: dict) -> str:
    if o["kind"] == "audio":
        return {"best": "Original audio", "vorbis": "OGG"}.get(o["afmt"], o["afmt"].upper()) if o["mode"] == "custom" else "MP3"
    if o["mode"] != "custom":
        return "MP4"
    return o["container"].upper() + ("" if o["res"] == "best" else f" {o['res']}p")


def shell_quote(a: str) -> str:
    return f'"{a}"' if re.search(r'[\s"]', a) else a


# ---------------------------------------------------------------- tools: yt-dlp / ffmpeg / JS runtime

tools = {
    "ytdlp": {"state": "checking", "version": None, "latest": None, "msg": "", "pct": None},
    "ffmpeg": {"state": "checking", "path": None, "msg": "", "pct": None},
    "js": {"state": "checking", "name": None, "path": None, "msg": "", "pct": None},
}
exe_lock = threading.Lock()  # held while a download runs; the updater takes it to swap the exe
tools_ready = threading.Event()
update_busy = threading.Event()


def fetch(url: str, dest: Path, key: str | None = None) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
            total, got = int(r.headers.get("Content-Length") or 0), 0
            while chunk := r.read(1 << 17):
                f.write(chunk)
                got += len(chunk)
                if key and total:
                    tools[key]["pct"] = round(got * 100 / total)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def latest_tag(repo: str) -> str:
    """Latest release tag via the /releases/latest redirect (no API rate limit)."""
    req = urllib.request.Request(f"https://github.com/{repo}/releases/latest", method="HEAD", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.geturl().rstrip("/").rsplit("/", 1)[-1]


def vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v))


def local_ytdlp_version() -> str | None:
    if not YTDLP.exists():
        return None
    try:
        p = subprocess.run([str(YTDLP), "--version"], capture_output=True, text=True, timeout=30,
                           creationflags=NOWIN, stdin=subprocess.DEVNULL)
        return p.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def update_ytdlp() -> None:
    t = tools["ytdlp"]
    t.update(state="checking", msg="Checking for updates", pct=None)
    t["version"] = local_ytdlp_version()
    try:
        latest = latest_tag("yt-dlp/yt-dlp")
        t["latest"] = latest
        if t["version"] and vkey(t["version"]) >= vkey(latest):
            t.update(state="ok", msg="Up to date")
            return
        t.update(state="updating", msg="Downloading yt-dlp " + latest, pct=0)
        base = f"https://github.com/yt-dlp/yt-dlp/releases/download/{latest}/"
        tmp = BIN / "yt-dlp.new"
        fetch(base + "yt-dlp.exe", tmp, "ytdlp")
        sums = {ln.split()[-1].lstrip("*"): ln.split()[0] for ln in fetch_text(base + "SHA2-256SUMS").splitlines() if ln.strip()}
        if hashlib.sha256(tmp.read_bytes()).hexdigest() != sums.get("yt-dlp.exe"):
            tmp.unlink(missing_ok=True)
            raise RuntimeError("checksum mismatch")
        t["msg"] = "Waiting for current download to finish" if exe_lock.locked() else "Installing"
        with exe_lock:
            os.replace(tmp, YTDLP)
        t.update(version=local_ytdlp_version(), state="ok", msg="Updated to " + latest, pct=None)
    except Exception as e:  # noqa: BLE001 - surface any failure in the UI, keep the old exe
        t.update(state="ok" if YTDLP.exists() else "missing", pct=None, msg=f"Update failed: {e}")


def ffmpeg_dir() -> Path | None:
    cands = [BIN]
    w = shutil.which("ffmpeg")
    if w:
        cands.append(Path(w).parent)
    return next((d for d in cands if (d / "ffmpeg.exe").exists() and (d / "ffprobe.exe").exists()), None)


def install_zip_tools(url: str, key: str, wanted: tuple[str, ...]) -> None:
    tmp = BIN / f"{key}.zip"
    fetch(url, tmp, key)
    try:
        with zipfile.ZipFile(tmp) as z:
            for name in z.namelist():
                base = name.rsplit("/", 1)[-1]
                if base.lower() in wanted:
                    with z.open(name) as src, open(BIN / (base + ".new"), "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    os.replace(BIN / (base + ".new"), BIN / base)
    finally:
        tmp.unlink(missing_ok=True)


def ensure_ffmpeg(force: bool = False) -> None:
    t = tools["ffmpeg"]
    d = None if force else ffmpeg_dir()
    if d is None:
        t.update(state="updating", msg="Downloading ffmpeg (one-time, ~130 MB)", pct=0)
        try:
            install_zip_tools(FFMPEG_ZIP, "ffmpeg", ("ffmpeg.exe", "ffprobe.exe"))
        except Exception as e:  # noqa: BLE001
            t.update(state="missing", pct=None, msg=f"ffmpeg download failed: {e}")
            return
        d = ffmpeg_dir()
    t.update(state="ok" if d else "missing", path=str(d) if d else None, pct=None, msg="Ready" if d else "Not found")


def find_js() -> tuple[str, str] | None:
    for name in ("deno", "node"):
        local = BIN / f"{name}.exe"
        p = str(local) if local.exists() else shutil.which(name)
        if p:
            return name, p
    return None


def ensure_js() -> None:
    t = tools["js"]
    found = find_js()
    if not found:
        t.update(state="updating", msg="Downloading Deno (one-time, for YouTube)", pct=0)
        try:
            install_zip_tools(DENO_ZIP, "js", ("deno.exe",))
        except Exception as e:  # noqa: BLE001
            t.update(state="missing", pct=None, msg=f"Deno download failed: {e}")
            return
        found = find_js()
    if found:
        t.update(state="ok", name=found[0], path=found[1], pct=None, msg=f"{found[0]} ready")
    else:
        t.update(state="missing", pct=None, msg="No JavaScript runtime found")


def js_args() -> list[str]:
    p = tools["js"]
    return ["--js-runtimes", f"{p['name']}:{p['path']}"] if p["state"] == "ok" and p["path"] else []


def ffmpeg_args() -> list[str]:
    p = tools["ffmpeg"]["path"]
    return ["--ffmpeg-location", p] if p else []


def refresh_tools(which: str = "all", force: bool = False) -> None:
    if update_busy.is_set():
        return
    update_busy.set()
    try:
        BIN.mkdir(exist_ok=True)
        if which in ("all", "ytdlp"):
            update_ytdlp()
        if which in ("all", "ffmpeg"):
            ensure_ffmpeg(force)
        if which in ("all", "js"):
            ensure_js()
    finally:
        update_busy.clear()
        tools_ready.set()


def startup_tools() -> None:
    if not YTDLP.exists() or cfg_snapshot()["auto_update"]:
        refresh_tools()
    else:
        tools["ytdlp"].update(state="ok", version=local_ytdlp_version(), msg="Auto-update is off")
        update_busy.set()
        try:
            ensure_ffmpeg()
            ensure_js()
        finally:
            update_busy.clear()
            tools_ready.set()


# ---------------------------------------------------------------- yt-dlp info

def clean_url(u: str) -> str:
    u = (u or "").strip()
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    if not re.match(r"^https?://[^\s]+$", u, re.I):
        raise ValueError("That doesn't look like a link.")
    return u


def last_error(text: str) -> str:
    errs = [ln.split("ERROR:", 1)[1].strip() for ln in text.splitlines() if "ERROR:" in ln]
    return re.sub(r"^\[[\w:]+\]\s*", "", errs[-1]) if errs else (text.strip().splitlines() or ["yt-dlp failed"])[-1]


def fetch_info(url: str) -> dict:
    if not YTDLP.exists():
        raise RuntimeError("yt-dlp isn't installed yet - still downloading it. Try again in a moment.")
    p = subprocess.run([str(YTDLP), "-J", "--no-playlist", "--flat-playlist", "--no-warnings", *js_args(), "--", url],
                       capture_output=True, timeout=120, creationflags=NOWIN, env=ENV, stdin=subprocess.DEVNULL)
    if p.returncode:
        raise RuntimeError(last_error(p.stderr.decode("utf-8", "replace")))
    return json.loads(p.stdout.decode("utf-8", "replace"))


def summarize(info: dict, url: str) -> dict:
    if info.get("_type") == "playlist":
        entries = []
        for e in info.get("entries") or []:
            if not e:
                continue
            u = e.get("url") or ""
            if not u.startswith("http"):
                u = f"https://www.youtube.com/watch?v={e.get('id')}"
            entries.append({"url": u, "title": e.get("title") or u, "thumb": (e.get("thumbnails") or [{}])[-1].get("url")})
        thumb = (info.get("thumbnails") or [{}])[-1].get("url") or (entries[0]["thumb"] if entries else None)
        return {"type": "playlist", "title": info.get("title") or "Playlist", "count": len(entries), "entries": entries,
                "uploader": info.get("uploader") or info.get("channel") or "", "thumb": thumb}
    return {"type": "video", "title": info.get("title") or url, "uploader": info.get("channel") or info.get("uploader") or "",
            "duration": info.get("duration"), "thumb": info.get("thumbnail"), "artist": guess_artist(info),
            "track": info.get("track"), "url": info.get("webpage_url") or url}


# ---------------------------------------------------------------- jobs

jobs: list[dict] = []
jobs_lock = threading.Lock()
pending: "queue.Queue[dict]" = queue.Queue()
procs: dict[str, subprocess.Popen] = {}

_STAGES = {"ExtractAudio": "Converting audio", "Merger": "Merging", "VideoConvertor": "Converting video",
           "EmbedThumbnail": "Adding cover art", "ThumbnailsConvertor": "Adding cover art",
           "Metadata": "Tagging", "FixupM4a": "Finishing", "FixupM3u8": "Finishing", "ModifyChapters": "Finishing"}


def public(j: dict) -> dict:
    return {k: v for k, v in j.items() if not k.startswith("_")}


def kill_tree(proc: subprocess.Popen) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, creationflags=NOWIN)


def run_job(j: dict) -> None:
    o = j["_opts"]
    root = Path(cfg_snapshot()["music_dir" if o["kind"] == "audio" else "video_dir"])
    j.update(state="running", stage="Preparing", progress=0)
    stem = "%(title)s"
    dest = root
    if o["sort_artist"]:
        title, track = j["title"], j.get("_track")
        if not j["artist"]:  # playlist item: work out the artist now
            info = fetch_info(j["url"])
            j["artist"] = guess_artist(info)
            title, track = info.get("title") or title, info.get("track")
        folder = j["folder"] or resolve_folder(root, j["artist"])[0]
        rel = safe_rel(folder)
        dest = root / rel
        j["folder"] = str(rel)
        stem = clean_stem(title, j["artist"], track).replace("%", "%%")
    dest.mkdir(parents=True, exist_ok=True)
    j["desc"] = describe(o) + (f" → {j['folder']}" if o["sort_artist"] else "")
    if j["_cancel"]:
        return

    cmd = [str(YTDLP), "--newline", "--no-mtime", "-N", "4", "--no-playlist", "--no-quiet",
           "--print", "after_move:TLFILE|%(filepath)s", *ffmpeg_args(), *js_args(), *build_args(o),
           "-P", str(dest), "-o", stem + ".%(ext)s", "--", j["url"]]
    streams, idx, err = 1, 0, ""
    with exe_lock:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=NOWIN, env=ENV)
        procs[j["id"]] = proc
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("TLFILE|"):
                j["file"] = line[7:]
            elif m := re.match(r"\[download\]\s+([\d.]+)%(?:\s+of\s+~?\s*\S+)?(?:\s+at\s+(\S+))?(?:\s+ETA\s+(\S+))?", line):
                j.update(stage="Downloading", speed=m[2] if m[2] and m[2] != "Unknown" else "", eta=m[3] or "",
                         progress=min(1.0, (idx + float(m[1]) / 100) / streams))
            elif line.startswith("[download] Destination:"):
                idx = max(0, idx + 1) if j.get("progress", 0) > 0 else 0
            elif m := re.search(r"Downloading (\d+) format\(s\): (\S+)", line):
                streams, idx = m[2].count("+") + 1, -1
            elif "has already been downloaded" in line:
                j.update(progress=1.0, stage="Already downloaded")
            elif m := re.match(r"\[(\w+)\]", line):
                if m[1] in _STAGES:
                    j.update(stage=_STAGES[m[1]], speed="", eta="", progress=1.0)
            if "ERROR:" in line:
                err = last_error(line)
        proc.wait()
        procs.pop(j["id"], None)
    if j["_cancel"]:
        return
    if proc.returncode == 0:
        j.update(state="done", stage="Done", progress=1.0, speed="", eta="")
    else:
        j.update(state="error", stage="Failed", error=err or f"yt-dlp exited with code {proc.returncode}")


def worker() -> None:
    tools_ready.wait()
    while True:
        j = pending.get()
        if j["_cancel"]:
            continue
        try:
            run_job(j)
        except Exception as e:  # noqa: BLE001
            j.update(state="error", stage="Failed", error=str(e))
        if j["_cancel"]:
            j.update(state="cancelled", stage="Cancelled", speed="", eta="")


def add_jobs(b: dict) -> int:
    o = clean_opts(b.get("opts") or {})
    root = Path(cfg_snapshot()["music_dir" if o["kind"] == "audio" else "video_dir"])
    artist, folder = (b.get("artist") or "").strip(), (b.get("folder") or "").strip()
    if o["sort_artist"] and artist and folder and b.get("remember") and norm(folder) != norm(artist):
        update_cfg({"artists": {**cfg_snapshot()["artists"], artist: folder}})
    items = b.get("entries") or [{"url": b.get("url"), "title": b.get("title"), "thumb": b.get("thumb")}]
    if o["sort_artist"] and artist and not folder:
        folder = resolve_folder(root, artist)[0]
    n = 0
    for it in items[:1000]:
        j = {"id": secrets.token_hex(4), "url": clean_url(it.get("url")), "title": it.get("title") or it.get("url"),
             "thumb": it.get("thumb"), "state": "queued", "stage": "Queued", "progress": 0, "speed": "", "eta": "",
             "artist": artist if o["sort_artist"] else "", "folder": folder if o["sort_artist"] else "",
             "desc": describe(o), "file": "", "error": "", "_opts": o, "_cancel": False, "_track": b.get("track")}
        with jobs_lock:
            jobs.append(j)
        pending.put(j)
        n += 1
    return n


def cancel_job(jid: str) -> None:
    with jobs_lock:
        j = next((x for x in jobs if x["id"] == jid), None)
    if not j or j["state"] not in ("queued", "running"):
        return
    j["_cancel"] = True
    if j["state"] == "queued":
        j.update(state="cancelled", stage="Cancelled")
    elif proc := procs.get(jid):
        kill_tree(proc)


def active() -> bool:
    return update_busy.is_set() or any(j["state"] in ("queued", "running") for j in jobs)


# ---------------------------------------------------------------- HTTP API

last_seen = time.time()
bye_at = 0.0
PORT = 0


def pick_folder(initial: str) -> str | None:
    py = Path(sys.executable).with_name("python.exe")
    code = ("import sys,tkinter as tk\nfrom tkinter import filedialog\nr=tk.Tk();r.withdraw();r.attributes('-topmost',True)\n"
            "print(filedialog.askdirectory(initialdir=sys.argv[1],title='Choose a folder',parent=r) or '')")
    p = subprocess.run([str(py if py.exists() else sys.executable), "-I", "-c", code, initial if Path(initial).is_dir() else str(Path.home())],
                       capture_output=True, text=True, creationflags=NOWIN, stdin=subprocess.DEVNULL)
    out = p.stdout.strip()
    return str(Path(out)) if out else None


def api_state(_b):
    return {"tools": tools, "jobs": [public(j) for j in jobs[-200:]], "busy": active()}


def api_info(b):
    url = clean_url(b.get("url"))
    return summarize(fetch_info(url), url)


def api_resolve(b):
    cfg = cfg_snapshot()
    root = Path(cfg["music_dir" if b.get("kind") == "audio" else "video_dir"])
    artist = (b.get("artist") or "").strip()
    if not artist:
        return {"folder": "", "how": "auto", "root": str(root), "folders": list_folders(root)}
    folder, how = resolve_folder(root, artist)
    return {"folder": folder, "how": how, "root": str(root), "folders": list_folders(root)}


def api_cmd(b):
    return {"cmd": "yt-dlp " + " ".join(shell_quote(a) for a in build_args(clean_opts(b.get("opts") or {})))}


def api_add(b):
    return {"added": add_jobs(b)}


def api_cancel(b):
    cancel_job(str(b.get("id")))
    return {}


def api_clear(_b):
    with jobs_lock:
        jobs[:] = [j for j in jobs if j["state"] in ("queued", "running")]
    return {}


def api_reveal(b):
    j = next((x for x in jobs if x["id"] == b.get("id")), None)
    if not j:
        return {}
    f = Path(j["file"]) if j["file"] else None
    if f and f.exists():
        subprocess.Popen(["explorer", "/select,", str(f)])
    elif f and f.parent.exists():
        os.startfile(f.parent)  # type: ignore[attr-defined]
    return {}


def api_update(b):
    which = b.get("what") if b.get("what") in ("ytdlp", "ffmpeg", "js") else "ytdlp"
    threading.Thread(target=refresh_tools, args=(which, which == "ffmpeg" and bool(b.get("force"))), daemon=True).start()
    return {}


POST_ROUTES = {
    "info": api_info, "resolve": api_resolve, "cmd": api_cmd, "add": api_add, "cancel": api_cancel,
    "clear": api_clear, "reveal": api_reveal, "update": api_update,
    "config": lambda b: update_cfg(b),
    "browse": lambda b: {"path": pick_folder(str(b.get("initial") or ""))},
    "ping": lambda b: {},
    "bye": lambda b: {},
}
GET_ROUTES = {"state": api_state, "config": lambda b: cfg_snapshot()}


class Handler(BaseHTTPRequestHandler):
    server_version = "TubeLoader"

    def log_message(self, *_):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"))

    def _authorized(self, query: dict) -> bool:
        # Loopback only + per-run token: stops other websites (CSRF / DNS rebinding) from driving the API.
        if self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):
            return False
        return secrets.compare_digest(self.headers.get("X-Token") or query.get("t", [""])[0], TOKEN)

    def _dispatch(self, routes: dict, name: str, body: dict) -> None:
        global last_seen, bye_at
        last_seen = time.time()
        bye_at = time.time() if name == "bye" else 0.0
        fn = routes.get(name)
        if not fn:
            return self._json(404, {"error": "not found"})
        try:
            self._json(200, fn(body))
        except (ValueError, RuntimeError) as e:
            self._json(400, {"error": str(e)})
        except subprocess.TimeoutExpired:
            self._json(400, {"error": "Timed out talking to YouTube."})
        except Exception as e:  # noqa: BLE001
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        u = urlparse(self.path)
        if self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost"):
            return self._send(403, b"forbidden", "text/plain")
        if u.path == "/":
            html = (UI_DIR / "index.html").read_text("utf-8").replace("__TOKEN__", TOKEN)
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if u.path in ("/icon.png", "/favicon.ico"):
            return self._send(200, (UI_DIR / "icon.png").read_bytes(), "image/png")
        if u.path == "/api/hello":
            return self._json(200, {"app": "tube-loader"})
        if u.path.startswith("/api/"):
            if not self._authorized(parse_qs(u.query)):
                return self._json(403, {"error": "forbidden"})
            return self._dispatch(GET_ROUTES, u.path[5:], {})
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        u = urlparse(self.path)
        if not u.path.startswith("/api/") or not self._authorized(parse_qs(u.query)):
            return self._json(403, {"error": "forbidden"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})
        self._dispatch(POST_ROUTES, u.path[5:], body if isinstance(body, dict) else {})


# ---------------------------------------------------------------- launcher

def open_window(url: str) -> None:
    pf = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LocalAppData")]
    rels = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"]
    for rel in rels:
        for base in pf:
            exe = Path(base) / rel
            if base and exe.exists():
                subprocess.Popen([str(exe), f"--app={url}", "--window-size=780,920", f"--user-data-dir={DATA / 'profile'}",
                                  "--no-first-run", "--no-default-browser-check"])
                return
    webbrowser.open(url)


def existing_instance() -> str | None:
    try:
        port = json.loads(INSTANCE_FILE.read_text("utf-8"))["port"]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/hello", timeout=1.5) as r:
            if json.load(r).get("app") == "tube-loader":
                return f"http://127.0.0.1:{port}/"
    except (OSError, ValueError, KeyError):
        pass
    return None


def watchdog(server: ThreadingHTTPServer) -> None:
    """Quit once the window is gone and nothing is left to do."""
    while True:
        time.sleep(2)
        now = time.time()
        gone = (bye_at and now - bye_at > 5) or now - last_seen > 180
        if gone and not active():
            INSTANCE_FILE.unlink(missing_ok=True)
            server.shutdown()
            return


def main() -> None:
    DATA.mkdir(exist_ok=True)
    BIN.mkdir(exist_ok=True)
    if url := existing_instance():
        return open_window(url)
    load_cfg()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    INSTANCE_FILE.write_text(json.dumps({"port": server.server_address[1]}), "utf-8")
    threading.Thread(target=startup_tools, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=watchdog, args=(server,), daemon=True).start()
    open_window(f"http://127.0.0.1:{server.server_address[1]}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
