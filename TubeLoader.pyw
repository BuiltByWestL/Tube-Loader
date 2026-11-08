"""Tube Loader - a tiny local front-end for yt-dlp. Standard library plus pywebview.

Runs on Windows and macOS. Double-click to run (no console). Starts a loopback-only HTTP server, opens the UI in
a native window, keeps yt-dlp / ffmpeg up to date from GitHub, and exits
when the window is closed and nothing is downloading.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import queue
import re
import secrets
import shutil
import signal
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FROZEN = getattr(sys, "frozen", False)  # running as the packaged TubeLoader.exe / Tube Loader.app
WIN, MAC = os.name == "nt", sys.platform == "darwin"
EXE = ".exe" if WIN else ""
APP = Path(__file__).resolve().parent
BUNDLE = Path(getattr(sys, "_MEIPASS", APP))  # where the UI files live
if not FROZEN:
    HOME = APP  # writable data
elif MAC:
    HOME = Path.home() / "Library" / "Application Support" / "TubeLoader"
else:
    HOME = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "TubeLoader"
UI_DIR, BIN, DATA = BUNDLE / "ui", HOME / "bin", HOME / "data"
LOCAL_YTDLP = BIN / ("yt-dlp" + EXE)
YTDLP = LOCAL_YTDLP  # re-pointed by locate_ytdlp() when a copy already exists on the system
CONFIG_FILE = DATA / "config.json"
INSTANCE_FILE = DATA / "instance.json"
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
VERSION = "1.2.1"
UA = f"TubeLoader/{VERSION}"
TOKEN = secrets.token_urlsafe(24)
if MAC:  # apps opened from Finder get a bare PATH; also look where Homebrew and MacPorts install tools
    os.environ["PATH"] = os.pathsep.join([os.environ.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin",
                                          "/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"])
ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
SSL_CTX = None  # Windows: Python reads the system certificate store
if MAC:  # the bundled Python can't see the Mac's certificates, so use certifi's
    try:
        import certifi
        SSL_CTX = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass

if MAC:
    _ARCH = "arm64" if platform.machine() == "arm64" else "amd64"
    YTDLP_ASSET = "yt-dlp_macos"  # universal build
    # Static macOS builds by Martin Riedl; each zip has a .sha256 next to the file the redirect lands on
    FFMPEG_ZIPS = [f"https://ffmpeg.martin-riedl.de/redirect/latest/macos/{_ARCH}/release/{n}.zip" for n in ("ffmpeg", "ffprobe")]
    FFMPEG_SUMS = True
    FFMPEG_SIZE = "~60 MB"
    DENO_ZIP = ("https://github.com/denoland/deno/releases/latest/download/deno-"
                + ("aarch64" if _ARCH == "arm64" else "x86_64") + "-apple-darwin.zip")
else:
    YTDLP_ASSET = "yt-dlp.exe"
    FFMPEG_ZIPS = ["https://github.com/yt-dlp/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"]
    FFMPEG_SUMS = False
    FFMPEG_SIZE = "~130 MB"
    DENO_ZIP = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"

# ---------------------------------------------------------------- config

DEFAULTS = {
    "music_dir": str(Path.home() / "Music"),
    "video_dir": str(Path.home() / ("Movies" if MAC else "Videos")),
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
        return a + (["--embed-thumbnail"] if o["thumb"] else []) + (["--embed-metadata"] if o["meta"] else [])
    if o["mode"] == "video":
        a = ["-S", "res,ext:mp4:m4a", "--recode-video", "mp4"]
        return a + (["--embed-thumbnail"] if o["thumb"] else []) + (["--embed-metadata"] if o["meta"] else [])
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
        elif o["container"] == "webm":
            sort.append("ext:webm:webm")
        a += ["-S", ",".join(sort), "--merge-output-format", o["container"]]
        if o["recode"]:
            a += ["--recode-video", o["container"]]
        if o["thumb"] and o["container"] != "webm":  # webm can't hold cover art
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
tools_ready = threading.Event()  # everything, incl. ffmpeg: downloads wait for this
core_ready = threading.Event()  # yt-dlp + JS runtime: enough to fetch video info
update_busy = threading.Event()


def fetch(url: str, dest: Path, key: str | dict | None = None) -> str:
    """Download url to dest and return the final URL after redirects. key: tool name (or status dict) to show progress on."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    bar = tools[key] if isinstance(key, str) else key
    try:
        with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r, open(dest, "wb") as f:
            total, got = int(r.headers.get("Content-Length") or 0), 0
            while chunk := r.read(1 << 17):
                f.write(chunk)
                got += len(chunk)
                if bar is not None and total:
                    bar["pct"] = round(got * 100 / total)
            return r.geturl()
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


def fetch_text(url: str) -> str:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30, context=SSL_CTX) as r:
        return r.read().decode("utf-8", "replace")


def latest_tag(repo: str) -> str:
    """Latest release tag via the /releases/latest redirect (no API rate limit)."""
    req = urllib.request.Request(f"https://github.com/{repo}/releases/latest", method="HEAD", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
        return r.geturl().rstrip("/").rsplit("/", 1)[-1]


def make_executable(p: Path) -> None:
    if not WIN:
        p.chmod(0o755)


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


def locate_ytdlp() -> None:
    """Our own copy wins, then one already installed on the system; otherwise we download one."""
    global YTDLP
    found = None if LOCAL_YTDLP.exists() else shutil.which("yt-dlp")
    YTDLP = Path(found) if found else LOCAL_YTDLP


def update_ytdlp() -> None:
    t = tools["ytdlp"]
    t.update(state="checking", msg="Checking for updates", pct=None)
    locate_ytdlp()
    t["version"] = local_ytdlp_version()
    if YTDLP != LOCAL_YTDLP:  # already on this PC: don't install another, just ask it to update itself
        try:
            with exe_lock:
                p = subprocess.run([str(YTDLP), "-U"], capture_output=True, text=True, timeout=180,
                                   creationflags=NOWIN, stdin=subprocess.DEVNULL)
            lines = [ln for ln in (p.stdout + p.stderr).splitlines() if ln.strip()]
            t.update(version=local_ytdlp_version(), state="ok", msg="Using yt-dlp found on this PC" + (f" - {lines[-1][:90]}" if lines else ""))
        except (OSError, subprocess.SubprocessError) as e:
            t.update(state="ok", msg=f"Using yt-dlp found on this PC ({e})")
        return
    try:
        latest = latest_tag("yt-dlp/yt-dlp")
        t["latest"] = latest
        if t["version"] and vkey(t["version"]) >= vkey(latest):
            t.update(state="ok", msg="Up to date")
            return
        t.update(state="updating", msg="Downloading yt-dlp " + latest, pct=0)
        base = f"https://github.com/yt-dlp/yt-dlp/releases/download/{latest}/"
        tmp = BIN / "yt-dlp.new"
        fetch(base + YTDLP_ASSET, tmp, "ytdlp")
        sums = {ln.split()[-1].lstrip("*"): ln.split()[0] for ln in fetch_text(base + "SHA2-256SUMS").splitlines() if ln.strip()}
        if hashlib.sha256(tmp.read_bytes()).hexdigest() != sums.get(YTDLP_ASSET):
            tmp.unlink(missing_ok=True)
            raise RuntimeError("checksum mismatch")
        make_executable(tmp)
        t["msg"] = "Waiting for current download to finish" if exe_lock.locked() else "Installing"
        with exe_lock:
            os.replace(tmp, LOCAL_YTDLP)
        t.update(version=local_ytdlp_version(), state="ok", msg="Updated to " + latest, pct=None)
    except Exception as e:  # noqa: BLE001 - surface any failure in the UI, keep the old exe
        t.update(state="ok" if YTDLP.exists() else "missing", pct=None, msg=f"Update failed: {e}")


def ffmpeg_dir() -> Path | None:
    cands = [BIN]
    w = shutil.which("ffmpeg")
    if w:
        cands.append(Path(w).parent)
    return next((d for d in cands if (d / f"ffmpeg{EXE}").exists() and (d / f"ffprobe{EXE}").exists()), None)


def install_zip_tools(url: str, key: str, wanted: tuple[str, ...], verify: bool = False) -> None:
    """Download a zip and unpack only the wanted files into BIN. verify: check <final url>.sha256 first."""
    tmp = BIN / f"{key}.zip"
    final = fetch(url, tmp, key)
    try:
        if verify and hashlib.sha256(tmp.read_bytes()).hexdigest() != fetch_text(final + ".sha256").split()[0].lower():
            raise RuntimeError("checksum mismatch")
        with zipfile.ZipFile(tmp) as z:
            for name in z.namelist():
                base = name.rsplit("/", 1)[-1]
                if base.lower() in wanted:
                    with z.open(name) as src, open(BIN / (base + ".new"), "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    make_executable(BIN / (base + ".new"))
                    os.replace(BIN / (base + ".new"), BIN / base)
    finally:
        tmp.unlink(missing_ok=True)


def ensure_ffmpeg(force: bool = False) -> None:
    t = tools["ffmpeg"]
    d = None if force else ffmpeg_dir()
    if d is None:
        t.update(state="updating", msg=f"Downloading ffmpeg (one-time, {FFMPEG_SIZE})", pct=0)
        try:
            for url in FFMPEG_ZIPS:
                install_zip_tools(url, "ffmpeg", (f"ffmpeg{EXE}", f"ffprobe{EXE}"), FFMPEG_SUMS)
        except Exception as e:  # noqa: BLE001
            t.update(state="missing", pct=None, msg=f"ffmpeg download failed: {e}")
            return
        d = ffmpeg_dir()
    t.update(state="ok" if d else "missing", path=str(d) if d else None, pct=None, msg="Ready" if d else "Not found")


def find_js() -> tuple[str, str] | None:
    for name in ("deno", "node"):
        local = BIN / f"{name}{EXE}"
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
            install_zip_tools(DENO_ZIP, "js", (f"deno{EXE}",))
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
        if which in ("all", "js"):
            ensure_js()
        core_ready.set()
        if which in ("all", "ffmpeg"):
            ensure_ffmpeg(force)
    finally:
        update_busy.clear()
        core_ready.set()
        tools_ready.set()


def startup_tools() -> None:
    locate_ytdlp()
    if not YTDLP.exists() or cfg_snapshot()["auto_update"]:
        refresh_tools()
    else:
        locate_ytdlp()
        tools["ytdlp"].update(state="ok", version=local_ytdlp_version(), msg="Auto-update is off")
        update_busy.set()
        try:
            ensure_js()
            core_ready.set()
            ensure_ffmpeg()
        finally:
            update_busy.clear()
            core_ready.set()
            tools_ready.set()


# ---------------------------------------------------------------- Tube Loader's own updates

REPO = "BuiltByWestL/Tube-Loader"
RELEASES = f"https://github.com/{REPO}/releases/latest"
APP_ASSET = "TubeLoader-mac.dmg" if MAC else "TubeLoader.exe"
BUNDLE_ID = "io.github.builtbywestl.tubeloader"
INSTALLS_FILE = DATA / "installs.json"  # where this app was last run from, and an old copy waiting for an answer
# state: idle | checking | current | available | downloading | installing | manual | error
app_update: dict = {"state": "idle", "latest": None, "msg": "", "pct": None}
old_copy: dict | None = None  # {"path", "version"}: an older Tube Loader the user hasn't decided about yet


def app_path() -> Path | None:
    """The .exe (Windows) or .app (Mac) this copy runs from. None when running from source."""
    if not FROZEN:
        return None
    p = Path(sys.executable).resolve()
    if MAC:
        p = p.parents[2]
        if p.suffix != ".app" or "/AppTranslocation/" in str(p):  # macOS runs quarantined apps from a random copy
            return None
    return p


def same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def is_tube_loader(p: Path) -> bool:
    """Only ever offer to remove something that's really a copy of this app."""
    if MAC:
        try:
            import plistlib
            with open(p / "Contents" / "Info.plist", "rb") as f:
                return plistlib.load(f).get("CFBundleIdentifier") == BUNDLE_ID
        except (OSError, ValueError):
            return False
    try:
        return p.is_file() and p.suffix.lower() == ".exe" and b"TubeLoader" in p.read_bytes()[-(8 << 20):]
    except OSError:
        return False


def release_info() -> dict:
    # TUBELOADER_UPDATE_FEED lets the tests point the updater at a local copy of the release.
    feed = os.environ.get("TUBELOADER_UPDATE_FEED", "")
    url = feed if feed.startswith("http://127.0.0.1:") else f"https://api.github.com/repos/{REPO}/releases/latest"
    rel = json.loads(fetch_text(url))
    asset = next((a for a in rel.get("assets", []) if a.get("name") == APP_ASSET), None)
    if not asset:
        raise RuntimeError(f"the latest release has no {APP_ASSET}")
    return {"version": str(rel["tag_name"]).lstrip("v"), "url": asset["browser_download_url"],
            "sha256": str(asset.get("digest") or "").removeprefix("sha256:").lower()}


def check_app(manual: bool = False) -> None:
    u = app_update
    if u["state"] in ("checking", "downloading", "installing"):
        return
    u.update(state="checking", msg="Checking for updates", pct=None)
    try:
        rel = release_info()
    except Exception as e:  # noqa: BLE001
        u.update(state="error" if manual else "idle", msg=f"Couldn't check for updates: {e}")
        return
    if vkey(rel["version"]) > vkey(VERSION):
        u.update(state="available", latest=rel["version"], msg=f"Tube Loader {rel['version']} is available", _rel=rel)
    else:
        u.update(state="current", latest=rel["version"], msg="Up to date")


def unique(p: Path) -> Path:
    n, q = 2, p
    while q.exists():
        q = p.with_name(f"{p.stem} ({n}){p.suffix}")
        n += 1
    return q


def swap_in_windows(me: Path, new: Path) -> Path:
    """Put the new exe where this one is. A running exe can be renamed, so this one steps aside."""
    staged = me.with_name(f"{me.stem}.update{me.suffix}")
    shutil.copyfile(new, staged)  # fails here, before touching anything, if the folder isn't writable
    old = unique(me.with_name(f"{me.stem} {VERSION}{me.suffix}"))
    os.replace(me, old)
    try:
        os.replace(staged, me)
    except OSError:
        os.replace(old, me)
        staged.unlink(missing_ok=True)
        raise
    return old


def swap_in_mac(me: Path, dmg: Path, work: Path) -> Path:
    mnt = work / "mnt"
    subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", str(mnt), str(dmg)],
                   check=True, capture_output=True, timeout=180)
    staged = me.with_name(f".{me.stem} update.app")
    shutil.rmtree(staged, ignore_errors=True)
    try:
        src = next(mnt.glob("*.app"))
        subprocess.run(["ditto", str(src), str(staged)], check=True, capture_output=True, timeout=600)
    finally:
        subprocess.run(["hdiutil", "detach", "-force", str(mnt)], capture_output=True, timeout=60)
    try:
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(staged)], check=True, capture_output=True, timeout=300)
        old = unique(me.with_name(f"{me.stem} {VERSION}.app"))
        os.rename(me, old)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    try:
        os.rename(staged, me)
    except OSError:
        os.rename(old, me)
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return old


def write_installs(rec: dict) -> None:
    try:
        INSTALLS_FILE.write_text(json.dumps(rec, indent=2), "utf-8")
    except OSError:
        pass


def relaunch(me: Path) -> None:
    """Start the new copy and quit. It waits for this process to be gone before it starts its server."""
    args = ["--after", str(os.getpid())]
    if MAC:
        subprocess.Popen(["open", "-n", str(me), "--args", *args])
    else:
        # A child of a one-file app inherits its unpack-folder settings: tell the new exe to start fresh
        env = {k: v for k, v in os.environ.items() if not k.startswith(("_PYI", "_MEI"))}
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        subprocess.Popen([str(me), *args], env=env, close_fds=True, stdin=subprocess.DEVNULL,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    time.sleep(0.5)
    quit_now()


def install_update() -> None:
    u, rel, me = app_update, app_update.get("_rel"), app_path()
    if not rel:
        return
    if not me:  # running from source, or a Mac app macOS is running from a quarantined copy
        webbrowser.open(RELEASES)
        u.update(state="manual", msg="Opened the download page in your browser")
        return
    if not rel["sha256"]:
        u.update(state="error", msg="The release has no checksum to check the download against")
        return
    work = Path(tempfile.mkdtemp(prefix="tubeloader-update-"))
    try:
        u.update(state="downloading", msg=f"Downloading Tube Loader {rel['version']}", pct=0)
        dl = work / APP_ASSET
        fetch(rel["url"], dl, u)
        if hashlib.sha256(dl.read_bytes()).hexdigest() != rel["sha256"]:
            raise RuntimeError("the download didn't match its checksum")
        if update_busy.is_set():  # don't quit in the middle of installing yt-dlp / ffmpeg / Deno
            u.update(msg="Waiting for the tools to finish updating", pct=None)
            while update_busy.is_set():
                time.sleep(0.5)
        if downloads_active():
            raise RuntimeError("downloads started while updating. Try again when they're done")
        u.update(state="installing", msg=f"Installing Tube Loader {rel['version']}", pct=None)
        try:
            old = swap_in_mac(me, dl, work) if MAC else swap_in_windows(me, dl)
        except (OSError, subprocess.SubprocessError, StopIteration) as e:
            # Can't replace this copy (e.g. a folder that needs admin rights): hand the new one over instead.
            saved = unique(Path.home() / "Downloads" / (f"TubeLoader-{rel['version']}-mac.dmg" if MAC else f"TubeLoader {rel['version']}.exe"))
            saved.parent.mkdir(exist_ok=True)
            shutil.copyfile(dl, saved)
            subprocess.Popen(["open", str(saved)] if MAC else ["explorer", "/select,", str(saved)])
            how = "Open it and drag Tube Loader into Applications" if MAC else "Run it from there"
            u.update(state="manual", msg=f"Couldn't update in place ({e}). Saved the new version to Downloads. {how}")
            return
        write_installs({"path": str(me), "version": rel["version"], "old": {"path": str(old), "version": VERSION}})
        u.update(msg=f"Restarting into Tube Loader {rel['version']}")
        relaunch(me)
    except Exception as e:  # noqa: BLE001
        u.update(state="error", pct=None, msg=f"Update failed: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def note_install() -> None:
    """Remember where this copy runs from. If an older copy was run from somewhere else (an update, or a newer
    version downloaded by hand), offer to remove it: the user decides, nothing is deleted automatically."""
    global old_copy
    me = app_path()
    if not me:
        return
    try:
        rec = json.loads(INSTALLS_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        rec = {}
    old = rec.get("old") if isinstance(rec.get("old"), dict) else None
    if not old and rec.get("path") and rec.get("version") and vkey(str(rec["version"])) < vkey(VERSION):
        old = {"path": str(rec["path"]), "version": str(rec["version"])}
    if old and (same_path(Path(old["path"]), me) or not is_tube_loader(Path(old["path"]))):
        old = None
    old_copy = old
    write_installs({"path": str(me), "version": VERSION, **({"old": old} if old else {})})


def move_to_trash(p: Path) -> None:
    if MAC:
        from Foundation import NSFileManager, NSURL
        ok, _, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
            NSURL.fileURLWithPath_(str(p)), None, None)
        if not ok:
            raise RuntimeError(str(err.localizedDescription()) if err else "couldn't move it to the Trash")
        return
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
    # FO_DELETE with FOF_ALLOWUNDO (Recycle Bin) | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
    op = SHFILEOPSTRUCTW(wFunc=3, pFrom=str(p) + "\0", fFlags=0x40 | 0x10 | 0x04 | 0x400)
    shell32 = ctypes.WinDLL("shell32")
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    rc = shell32.SHFileOperationW(ctypes.byref(op))
    if rc or op.fAnyOperationsAborted or p.exists():
        raise RuntimeError(f"couldn't move it to the Recycle Bin (error {rc})")


def answer_old_copy(action: str) -> dict:
    global old_copy
    old = old_copy
    if not old:
        return {}
    if action == "delete":
        p = Path(old["path"])
        if p.exists() and is_tube_loader(p):
            for attempt in range(10):  # the old copy may still be closing
                try:
                    move_to_trash(p)
                    break
                except RuntimeError:
                    if attempt == 9:
                        raise
                    time.sleep(0.5)
    old_copy = None
    me = app_path()
    write_installs({"path": str(me), "version": VERSION} if me else {})
    return {}


def wait_for_exit(pid: int, limit: float = 20) -> None:
    """--after <pid>: the copy being replaced is still shutting down."""
    if WIN:
        import ctypes
        k = ctypes.WinDLL("kernel32")
        h = k.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if h:
            k.WaitForSingleObject(h, int(limit * 1000))
            k.CloseHandle(h)
        return
    end = time.time() + limit
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        time.sleep(0.1)


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
    core_ready.wait(180)  # first launch: yt-dlp is still being downloaded
    if not YTDLP.exists():
        raise RuntimeError("yt-dlp couldn't be installed - check your internet connection, then Settings -> Check for updates.")
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
    if WIN:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, creationflags=NOWIN)
        return
    try:  # yt-dlp runs in its own process group, so this takes ffmpeg down with it
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass


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
    j["_dest"] = str(dest)
    j["desc"] = describe(o) + (f" → {j['folder']}" if o["sort_artist"] else "")
    if j["_cancel"]:
        return

    cmd = [str(YTDLP), "--newline", "--no-mtime", "-N", "4", "--no-playlist", "--no-quiet",
           "--print", "after_move:TLFILE|%(filepath)s", *ffmpeg_args(), *js_args(), *build_args(o),
           "-P", str(dest), "-o", stem + ".%(ext)s", "--", j["url"]]
    streams, idx, err = 1, 0, ""
    with exe_lock:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                creationflags=NOWIN, env=ENV, start_new_session=not WIN)
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


def retry_job(jid: str) -> None:
    with jobs_lock:
        j = next((x for x in jobs if x["id"] == jid), None)
    if j and j["state"] in ("error", "cancelled"):
        j.update(state="queued", stage="Queued", progress=0, speed="", eta="", error="", file="", _cancel=False)
        pending.put(j)


def downloads_active() -> bool:
    return any(j["state"] in ("queued", "running") for j in jobs)


def active() -> bool:
    return update_busy.is_set() or downloads_active()


# ---------------------------------------------------------------- HTTP API

last_seen = time.time()
bye_at = 0.0
window_open = threading.Event()
PORT = 0


def read_clipboard() -> str:
    # Read it natively: the web view's clipboard API asks for permission every session on Windows
    # and does nothing in the Mac web view.
    if MAC:
        r = subprocess.run(["pbpaste"], capture_output=True, timeout=5, stdin=subprocess.DEVNULL,
                           env={**os.environ, "LANG": "en_US.UTF-8"})
        return r.stdout.decode("utf-8", "replace")
    if not WIN:
        return ""
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.GetClipboardData.argtypes, user32.GetClipboardData.restype = [wintypes.UINT], wintypes.HANDLE
    kernel32.GlobalLock.argtypes, kernel32.GlobalLock.restype = [wintypes.HGLOBAL], wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    for _ in range(10):  # another app may be holding the clipboard for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return ""
    try:
        handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
        ptr = kernel32.GlobalLock(handle) if handle else None
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def pick_folder(initial: str) -> str | None:
    start = initial if Path(initial).is_dir() else str(Path.home())
    if MAC:
        return pick_folder_mac(start)
    out = Path(tempfile.gettempdir()) / f"tubeloader_pick_{secrets.token_hex(4)}.txt"
    if FROZEN:
        cmd = [sys.executable, "--pick-folder", start, str(out)]
    else:
        py = Path(sys.executable).with_name("python.exe")
        cmd = [str(py if py.exists() else sys.executable), str(Path(__file__).resolve()), "--pick-folder", start, str(out)]
    subprocess.run(cmd, creationflags=NOWIN, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   env={**ENV, "PYINSTALLER_RESET_ENVIRONMENT": "1"})
    try:
        chosen = out.read_text("utf-8").strip()
    except OSError:
        chosen = ""
    out.unlink(missing_ok=True)
    return str(Path(chosen)) if chosen else None


def pick_folder_mac(start: str) -> str | None:
    """Native folder picker on the app window, or AppleScript's when there's no window (browser fallback)."""
    try:
        import webview
        if webview.windows:
            got = webview.windows[0].create_file_dialog(webview.FileDialog.FOLDER, directory=start)
            return str(Path(got[0])) if got else None
    except Exception:  # noqa: BLE001
        pass
    script = ["-e", "on run argv", "-e", 'POSIX path of (choose folder with prompt "Choose a folder" '
              "default location (POSIX file (item 1 of argv)))", "-e", "end run"]
    p = subprocess.run(["osascript", *script, start], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    chosen = p.stdout.strip() if p.returncode == 0 else ""
    return str(Path(chosen)) if chosen else None


def pick_folder_dialog(initial: str, out: str) -> None:
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    Path(out).write_text(filedialog.askdirectory(initialdir=initial, title="Choose a folder", parent=root) or "", "utf-8")


def api_state(_b):
    return {"tools": tools, "jobs": [public(j) for j in jobs[-200:]], "busy": active(), "version": VERSION,
            "app": {k: v for k, v in app_update.items() if not k.startswith("_")}, "old_copy": old_copy,
            "trash": "Trash" if MAC else "Recycle Bin"}


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


def api_retry(b):
    retry_job(str(b.get("id")))
    return {}


def api_remove(b):
    with jobs_lock:
        jobs[:] = [j for j in jobs if j["id"] != b.get("id") or j["state"] in ("queued", "running")]
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
    folder = f.parent if f else Path(j.get("_dest") or ".")
    if f and f.exists():
        subprocess.Popen(["open", "-R", str(f)] if MAC else ["explorer", "/select,", str(f)])
    elif j.get("_dest") and folder.exists():
        if MAC:
            subprocess.Popen(["open", str(folder)])
        else:
            os.startfile(folder)  # type: ignore[attr-defined]
    return {}


def api_update(b):
    which = b.get("what") if b.get("what") in ("ytdlp", "ffmpeg", "js") else "ytdlp"
    threading.Thread(target=refresh_tools, args=(which, which == "ffmpeg" and bool(b.get("force"))), daemon=True).start()
    return {}


def api_update_app(_b):
    if downloads_active():
        raise RuntimeError("Wait for your downloads to finish, then update.")
    if app_update["state"] != "available":
        raise RuntimeError("There's no update to install.")
    threading.Thread(target=install_update, daemon=True).start()
    return {}


def api_check_app(_b):
    threading.Thread(target=check_app, args=(True,), daemon=True).start()
    return {}


def api_open_release(_b):
    webbrowser.open(RELEASES)
    return {}


POST_ROUTES = {
    "info": api_info, "resolve": api_resolve, "cmd": api_cmd, "add": api_add, "cancel": api_cancel,
    "clear": api_clear, "retry": api_retry, "remove": api_remove, "reveal": api_reveal, "update": api_update,
    "config": lambda b: update_cfg(b),
    "browse": lambda b: {"path": pick_folder(str(b.get("initial") or ""))},
    "clipboard": lambda b: {"text": read_clipboard()[:4000]},
    "check_app": api_check_app,
    "update_app": api_update_app,
    "old_copy": lambda b: answer_old_copy(str(b.get("action"))),
    "open_release": api_open_release,
    "ping": lambda b: {},
    "bye": lambda b: {},
}
GET_ROUTES = {"state": api_state, "config": lambda b: cfg_snapshot()}


class Handler(BaseHTTPRequestHandler):
    server_version = "TubeLoader"

    def log_message(self, *_):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json", cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
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
            theme = ((cfg_snapshot()["ui"].get("S") or {}).get("theme"))
            html = (UI_DIR / "index.html").read_text("utf-8").replace("__TOKEN__", TOKEN)
            html = html.replace("__THEME__", theme if theme in ("light", "dark") else "auto")
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if u.path in ("/icon.png", "/favicon.ico"):
            return self._send(200, (UI_DIR / "icon.png").read_bytes(), "image/png")
        if re.fullmatch(r"/fonts/[\w-]+\.woff2", u.path):
            f = UI_DIR / u.path[1:]
            if f.exists():
                return self._send(200, f.read_bytes(), "font/woff2", "max-age=31536000")
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

def guard_close(win) -> None:
    """Mac: closing the window quits the app, so ask first while downloads are still going."""
    while window_open.is_set():
        win.confirm_close = any(j["state"] in ("queued", "running") for j in jobs)
        time.sleep(0.5)


def watch_app_quit():
    """Mac: Cmd+Q ends the process without returning from webview.start(), so stop downloads from here too."""
    try:
        from AppKit import NSApplicationWillTerminateNotification
        from Foundation import NSNotificationCenter
        return NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
            NSApplicationWillTerminateNotification, None, None, lambda _note: quit_now())
    except Exception:  # noqa: BLE001
        return None


def run_window(url: str) -> bool:
    """Native app window (WebView2 / WebKit), blocking until it's closed. Default browser if that isn't available.
    Returns True if the native window was used."""
    window_open.set()
    try:
        import webview
        win = webview.create_window("Tube Loader", url, width=1040, height=780, min_size=(560, 520))
        if MAC:
            threading.Thread(target=guard_close, args=(win,), daemon=True).start()
            on_quit = watch_app_quit()  # keep a reference so the observer stays alive
            webview.start(localization={"global.quitConfirmation": "Downloads are still running. Quit and stop them?"})
        else:
            webview.start(gui="edgechromium")
    except Exception:  # noqa: BLE001 - no pywebview / no WebView2 runtime
        window_open.clear()
        webbrowser.open(url)
        return False
    window_open.clear()
    return True


def quit_now() -> None:
    """Mac: reopening the app can't bring back a closed window, so closing it quits for real."""
    for j in list(jobs):
        j["_cancel"] = True
    for proc in list(procs.values()):
        kill_tree(proc)
    INSTANCE_FILE.unlink(missing_ok=True)
    os._exit(0)


def existing_instance() -> str | None:
    try:
        port = json.loads(INSTANCE_FILE.read_text("utf-8"))["port"]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/hello", timeout=1.5) as r:
            if json.load(r).get("app") == "tube-loader":
                return f"http://127.0.0.1:{port}/"
    except (OSError, ValueError, KeyError):
        pass
    return None


class LocalServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # HTTPServer looks up the machine's name for 127.0.0.1, which can hang for ~30 s on a Mac. We never use it.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


def watchdog(server: ThreadingHTTPServer) -> None:
    """Quit once the window is gone and nothing is left to do."""
    while True:
        time.sleep(2)
        now = time.time()
        gone = (bye_at and now - bye_at > 5) or now - last_seen > 180
        if gone and not window_open.is_set() and not active():
            INSTANCE_FILE.unlink(missing_ok=True)
            server.shutdown()
            return


def main() -> None:
    global bye_at
    if len(sys.argv) == 4 and sys.argv[1] == "--pick-folder":
        return pick_folder_dialog(sys.argv[2], sys.argv[3])
    if len(sys.argv) >= 3 and sys.argv[1] == "--after" and sys.argv[2].isdigit():
        wait_for_exit(int(sys.argv[2]))  # just updated: let the old copy finish quitting
    DATA.mkdir(parents=True, exist_ok=True)
    BIN.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(DATA / "profile", ignore_errors=True)  # Edge profile from versions before the native window
    if url := existing_instance():
        run_window(url)
        return
    load_cfg()
    note_install()
    server = LocalServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    INSTANCE_FILE.write_text(json.dumps({"port": server.server_address[1]}), "utf-8")
    threading.Thread(target=startup_tools, daemon=True).start()
    if cfg_snapshot()["auto_update"]:
        threading.Thread(target=check_app, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=watchdog, args=(server,), daemon=True).start()
    serving = threading.Thread(target=server.serve_forever)
    serving.start()
    if run_window(f"http://127.0.0.1:{server.server_address[1]}/") and MAC:
        quit_now()
    if not bye_at:
        bye_at = time.time()  # window closed: finish any downloads, then the watchdog stops the server
    serving.join()


if __name__ == "__main__":
    main()
