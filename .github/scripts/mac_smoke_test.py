"""End-to-end check of the installed Mac app, as a user would run it.

Opens /Applications/Tube Loader.app, waits for it to install yt-dlp / ffmpeg / Deno, downloads a song and a
video through the app's own API, cancels a download, then quits the app. Screenshots and a JSON report
land in $OUT (default ./smoke). Exit code 0 only if every required step passed.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP = "/Applications/Tube Loader.app"
SUPPORT = Path.home() / "Library/Application Support/TubeLoader"
OUT = Path(os.environ.get("OUT", "smoke")).resolve()
OUT.mkdir(parents=True, exist_ok=True)
MEDIA = OUT / "media"
URLS = [  # first one that yt-dlp can fetch from this machine is used
    "https://www.youtube.com/watch?v=jNQXAC9IVRw",  # Me at the zoo, 19 s
    "https://vimeo.com/76979871",
    "https://soundcloud.com/ethmusic/lostin-powers-she-so-heavy",
]
report = {"steps": [], "ok": True}
base = token = None


def step(name, ok, detail="", required=True):
    report["steps"].append({"step": name, "ok": ok, "required": required, "detail": detail})
    if required and not ok:
        report["ok"] = False
    print(("PASS " if ok else ("FAIL " if required else "WARN ")) + name + (f" - {detail}" if detail else ""), flush=True)


def shot(name):
    subprocess.run(["screencapture", "-x", str(OUT / f"{name}.png")])


def api(path, body=None, timeout=150):
    req = urllib.request.Request(base + "api/" + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Token": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": json.load(e).get("error", str(e))}


def wait_job(jid, limit=600):
    end = time.time() + limit
    while time.time() < end:
        j = next((x for x in api("state")["jobs"] if x["id"] == jid), None)
        if j and j["state"] not in ("queued", "running"):
            return j
        time.sleep(2)
    return j


def probe(f):
    ff = Path(api("state")["tools"]["ffmpeg"]["path"]) / "ffprobe"
    p = subprocess.run([str(ff), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(f)],
                       capture_output=True, text=True)
    return json.loads(p.stdout or "{}")


def main():
    global base, token
    subprocess.run(["sw_vers"])
    subprocess.run(["uname", "-m"])
    sig = subprocess.run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", APP], capture_output=True, text=True)
    step("code signature intact", sig.returncode == 0, (sig.stdout + sig.stderr).strip()[-300:])

    subprocess.run(["open", APP], check=True)
    inst = SUPPORT / "data/instance.json"
    for _ in range(60):
        if inst.exists():
            break
        time.sleep(1)
    step("app started (local server up)", inst.exists())
    if not inst.exists():
        shot("not-started")
        return
    base = f"http://127.0.0.1:{json.loads(inst.read_text())['port']}/"
    html = urllib.request.urlopen(base, timeout=10).read().decode()
    token = re.search(r'const TOKEN = "([^"]+)"', html)[1]
    step("UI served", "Tube Loader" in html)

    time.sleep(4)
    shot("1-first-launch")
    end = time.time() + 900
    while time.time() < end:
        t = api("state")["tools"]
        if all(t[k]["state"] in ("ok", "missing") for k in t):
            break
        time.sleep(3)
    report["tools"] = t
    for k in ("ytdlp", "ffmpeg", "js"):
        step(f"{k} installed", t[k]["state"] == "ok", t[k].get("msg", ""))
    for name in ("yt-dlp", "ffmpeg", "ffprobe", "deno"):
        f = SUPPORT / "bin" / name
        if f.exists():
            p = subprocess.run([str(f), "--version" if name in ("yt-dlp", "deno") else "-version"], capture_output=True, text=True)
            arch = subprocess.run(["file", "-b", str(f)], capture_output=True, text=True).stdout.strip()
            step(f"{name} runs", p.returncode == 0, f"{(p.stdout or p.stderr).splitlines()[0][:70]} | {arch[:90]}")
    shot("2-tools-ready")

    MEDIA.mkdir(exist_ok=True)
    cfg = api("config", {"music_dir": str(MEDIA / "Music"), "video_dir": str(MEDIA / "Movies")})
    step("settings saved", cfg.get("music_dir") == str(MEDIA / "Music"))

    info = url = None
    for u in URLS:
        r = api("info", {"url": u})
        ok = "error" not in r
        step(f"fetch info: {u}", ok, r.get("error", r.get("title", "")), required=False)
        if ok:
            info, url = r, u
            break
    report["source"] = url
    step("could fetch a video from some site", info is not None)
    if not info:
        return
    report["youtube_ok"] = url == URLS[0]

    r = api("add", {"url": info["url"], "title": info["title"], "thumb": info.get("thumb"), "track": info.get("track"),
                    "artist": info.get("artist", ""), "folder": "", "remember": False, "opts": {"mode": "song", "thumb": True}})
    time.sleep(1)
    j = wait_job(api("state")["jobs"][-1]["id"])
    step("song download", j["state"] == "done", j.get("error") or j.get("file", ""))
    if j["state"] == "done":
        f = Path(j["file"])
        pr = probe(f)
        kinds = [s.get("codec_name") for s in pr.get("streams", [])]
        step("song is an mp3 with cover art, in an artist folder",
             f.suffix == ".mp3" and "mp3" in kinds and any(k in ("mjpeg", "png") for k in kinds) and f.parent.parent == MEDIA / "Music",
             f"{f.relative_to(MEDIA)} streams={kinds}")
    shot("3-after-song")

    api("add", {"url": info["url"], "title": info["title"], "thumb": info.get("thumb"), "opts": {"mode": "video"}})
    time.sleep(1)
    j = wait_job(api("state")["jobs"][-1]["id"])
    step("video download", j["state"] == "done", j.get("error") or j.get("file", ""))
    if j["state"] == "done":
        f = Path(j["file"])
        kinds = [s.get("codec_type") for s in probe(f).get("streams", [])]
        step("video is an mp4 with video and audio", f.suffix == ".mp4" and "video" in kinds and "audio" in kinds,
             f"{f.name} {kinds}")
        r = api("reveal", {"id": j["id"]})
        step("show in Finder", "error" not in r, json.dumps(r))
        time.sleep(2)
        shot("4-reveal-in-finder")
        subprocess.run(["osascript", "-e", 'tell application "Finder" to close every window'])

    api("add", {"url": info["url"], "title": info["title"], "opts": {"mode": "custom", "kind": "video", "res": "best", "recode": True}})
    time.sleep(1)
    jid = api("state")["jobs"][-1]["id"]
    for _ in range(60):
        st = next(x for x in api("state")["jobs"] if x["id"] == jid)
        if st["state"] == "running":
            break
        time.sleep(0.5)
    time.sleep(2)
    api("cancel", {"id": jid})
    time.sleep(3)
    st = next(x for x in api("state")["jobs"] if x["id"] == jid)
    left = subprocess.run(["pgrep", "-f", "TubeLoader/bin/(yt-dlp|ffmpeg)"], capture_output=True, text=True).stdout.split()
    step("cancel stops the download and its ffmpeg", st["state"] == "cancelled" and not left, f"state={st['state']} leftover={left}")

    subprocess.run(["osascript", "-e", 'tell application "Tube Loader" to activate'])
    time.sleep(2)
    shot("5-window")

    # Quit while downloads are queued: the app should ask first, and quitting should stop yt-dlp / ffmpeg.
    for _ in range(3):
        api("add", {"url": info["url"], "title": info["title"], "opts": {"mode": "custom", "kind": "video", "recode": True, "container": "mkv"}})
    time.sleep(3)
    quit_app()
    time.sleep(3)
    shot("6-quit-while-downloading")
    asked = running()
    step("asks before quitting while downloads run", asked, required=False)
    if asked:
        subprocess.run(["osascript", "-e", 'tell application "System Events" to keystroke return'], capture_output=True)
        confirmed = wait_gone(15)
        if confirmed:
            time.sleep(2)
            left = subprocess.run(["pgrep", "-f", "TubeLoader/bin/(yt-dlp|ffmpeg)"], capture_output=True, text=True).stdout.split()
            step("quitting stops running downloads", not left, f"leftover={left}")
        else:
            step("confirm quit dialog by keyboard (needs accessibility on the runner)", False, required=False)
            subprocess.run(["pkill", "-9", "-f", "Tube Loader.app/Contents/MacOS"])
            subprocess.run(["pkill", "-9", "-f", "TubeLoader/bin/"])
            time.sleep(2)

    # Second launch: tools already there, should be ready almost at once; then a plain quit.
    subprocess.run(["open", APP], check=True)
    t0 = time.time()
    while time.time() - t0 < 60:
        try:
            base = f"http://127.0.0.1:{json.loads(inst.read_text())['port']}/"
            token = re.search(r'const TOKEN = "([^"]+)"', urllib.request.urlopen(base, timeout=3).read().decode())[1]
            t = api("state")["tools"]
            if all(t[k]["state"] == "ok" for k in t):
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    step("second launch reuses installed tools", all(t[k]["state"] == "ok" for k in t), f"{time.time() - t0:.0f} s")
    time.sleep(3)
    shot("7-second-launch")
    quit_app()
    step("quits cleanly", wait_gone(20) and not inst.exists())


def running():
    return bool(subprocess.run(["pgrep", "-f", "Tube Loader.app/Contents/MacOS"], capture_output=True).stdout)


def wait_gone(limit):
    for _ in range(limit):
        if not running():
            return True
        time.sleep(1)
    return not running()


def quit_app():
    subprocess.run(["osascript", "-e", "ignoring application responses", "-e", 'tell application "Tube Loader" to quit',
                    "-e", "end ignoring"])


try:
    main()
except Exception as e:  # noqa: BLE001
    step("smoke test crashed", False, f"{type(e).__name__}: {e}")
    shot("crash")
(OUT / "report.json").write_text(json.dumps(report, indent=2))
sys.exit(0 if report["ok"] else 1)
