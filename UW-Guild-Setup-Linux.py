#!/usr/bin/env python3
# UW Guild addon setup for Linux: WoW Forever run through Wine, Proton (Steam), Lutris or Bottles.
# Free, from the guild's own site, no account. Run it once from a terminal:   python3 -I ~/Downloads/UW-Guild-Setup-Linux.py
# (-I: Python then loads nothing from the Downloads folder but this file, so a stray .py there can't run first.)
#
# The same setup as the Windows one (site/UW-Guild-Setup.cmd), with the same rules. What it does, and nothing else:
#   - finds WoW Forever's AddOns folder inside your Wine prefix (Lutris, Steam's Proton, Bottles, plain Wine, or the
#     Battle.net app's own list in that prefix), or asks once;
#   - downloads version.json and UW_Guild.zip from https://unbotheredwarlords.github.io, checks version.json's
#     signature against the guild's key below, the zip's size and SHA-256 against version.json, and that every file in
#     it is a WoW addon file inside UW_Guild/, then swaps the new UW_Guild folder in (the old one comes back if
#     anything fails);
#   - turns on a systemd timer for your user (no sudo) that checks once an hour, and installs an update only while
#     WoW Forever is closed, so the game never loads half an update.
# It never touches WTF (your settings and progress), never downgrades, never downloads or runs any program, and never
# updates itself: a new version of this setup is a new download. Its own files live in ~/.local/share/uw-guild (copy of
# this script, settings, log). Remove it with your app menu's "Stop UW Guild addon automatic updates", or run this file
# with --remove (the addon stays).
#
# Python 3.8 or newer, standard library only (Linux Mint 20 and later ship it). Options: --quiet (the timer: no output,
# no questions), --remove, --addons <folder> (skip the search), --no-schedule (install once, set up nothing),
# --test-run (tests only: needs --addons, --state-dir and --source, sets up nothing).
from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import http.client
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ADDON = "UW_Guild"
SOURCE = "https://unbotheredwarlords.github.io"
UNIT = "uw-guild-updater"
# Only file types WoW loads (bot/uwbot/addonzip.py SHIP): nothing else is ever written.
SHIP = (".toc", ".lua", ".xml", ".tga", ".blp", ".ttf", ".ogg", ".mp3", ".md", ".txt")
MAX_ZIP = 20 * 1024 * 1024
MAX_UNPACKED = 60 * 1024 * 1024
# The site's signing key, the same one UW-Guild-Setup.cmd pins (public half; the private half lives only on the officer
# PC, where the site is published). version.json.sig = base64 RSA PKCS#1 v1.5 SHA-256 signature over version.json's
# exact bytes, and version.json pins the zip's SHA-256, so a push to the site repo without that key can't reach any
# install. Empty would mean hash checks only, and no timer.
PUBKEY = "<RSAKeyValue><Modulus>rdpU9GcizmAFLkbZiYf8LTWWr1iXzL34zIRT87jyFd+g6NXY7kQz9PjahF8QLdiHOUSa/YH0XOFAD8BF5+1zLSulYsPelTTK8Cxyl4uq37mQI6vCma6tP0sNvoD2i1u+bvmzGq38yOkCojdZby+kumuOCitjWZn20QPJYcW5ftIUS4YUKe8hW5M6KD6EmeZLEH7sVTM96YDG3ynJmJslXS4dJA31GBXE3NcOH22abLHcTqefPRmrZxMGTxB5MOtffVOnGihVyHShVdIAwdyDOgse0D+ZIIfOvnir1NfmuDYFQ38sgNATVCnQSn9pn2V8VrbXGgzbP8cNqO63bYzhfe58DBM/+pNwR6xBLRkNo/8W1+Lmh6yQmHOjRyiFfT0AvN3tcqrhyUxw4Keq3VTWedwJ4b1cap5A8HDkVBtoqpd7ULmLz+tR8b5SF3xsLarweodbtZ6t61WRSsYDjM6D8/hBkMeNH0QafpjWKjVK/vzq6sGKPkLs3udm7Mo9Kb55</Modulus><Exponent>AQAB</Exponent></RSAKeyValue>"
VERSION_RX = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})(?:\.(\d{4}))?-([0-9a-f]{7})$")
# A Forever client in the process list. Under Wine its command line is the Windows path (C:\...\_forever_\Wow.exe).
GAME_RX = re.compile(rb"[\\/]_forever_[\\/][^\\/\x00]*\.exe", re.IGNORECASE)

args: argparse.Namespace
STATE: Path
LOG: Path
CONFIG: Path
SCRIPT_COPY: Path
HOME = Path.home()
UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "systemd" / "user"
MENU_DIR = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local" / "share") / "applications"
MENU = {"uw-guild-update.desktop": ("Update UW Guild addon now", "Check for a new UW_Guild and install it now", ""),
        "uw-guild-stop.desktop": ("Stop UW Guild addon automatic updates",
                                  "Stop updating UW_Guild automatically (the addon stays)", " --remove")}


class Problem(Exception):
    pass


def say(text: str = "") -> None:
    if not args.quiet:
        print(text, flush=True)


def log(text: str) -> None:
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        old = LOG.read_text(encoding="utf-8").splitlines()[-199:] if LOG.exists() else []
        line = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + "Z " + text
        LOG.write_text("\n".join(old + [line]) + "\n", encoding="utf-8")
    except OSError:
        pass


def done(code: int) -> None:
    # From the app menu the terminal closes on exit: give it time to be read. Typed in a terminal, it just ends.
    if args.from_menu and not args.quiet:
        print()
        if code == 0:
            print("This window closes in 20 seconds.", flush=True)
            time.sleep(20)
        else:
            try:
                input("Press Enter to close")
            except EOFError:
                pass
    sys.exit(code)


def fail(text: str) -> None:
    log("FAILED: " + text)
    say()
    say(text)
    done(1)


def version_key(v: str) -> int:
    # 2026.11.08.1553-61841f3 -> 202611081553 (older builds without the time read as 0000). Anything else: 0.
    m = VERSION_RX.match(str(v or ""))
    if not m:
        return 0
    return int(m.group(1) + m.group(2) + m.group(3) + (m.group(4) or "0000"))


def toc_version(toc: Path) -> str:
    try:
        for line in toc.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^\s*##\s*Version\s*:\s*(\S+)", line)
            if m:
                return m.group(1)
    except OSError:
        pass
    return ""


def read_config() -> dict:
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


# --- Finding WoW Forever ------------------------------------------------------------------------------------------
# Linux folders are case-sensitive and Wine's are not, so every step below matches names the way Wine does.

def child(folder: Path, name: str) -> Path | None:
    """folder/name, matched without regard to case (an existing exact match first)."""
    exact = folder / name
    if exact.exists():
        return exact
    try:
        for entry in os.scandir(folder):
            if entry.name.lower() == name.lower():
                return folder / entry.name
    except OSError:
        pass
    return None


def ci_path(folder: Path, rel: str) -> Path | None:
    p: Path | None = folder
    for part in [x for x in re.split(r"[\\/]", rel) if x]:
        p = child(p, part) if p else None
    return p


def forever_from(path: Path | str | None) -> Path | None:
    """A folder somewhere inside a World of Warcraft install -> its _forever_ folder, if there is one."""
    if not path:
        return None
    p = Path(str(path).strip().strip("'\"")).expanduser()
    for _ in range(4):
        if p.name.lower() == "_forever_" and p.is_dir():
            return p
        f = child(p, "_forever_")
        if f and f.is_dir():
            return f
        if p.parent == p:
            break
        p = p.parent
    return None


def prefix_drive(prefix: Path, letter: str) -> Path | None:
    """A Wine prefix's drive (C: -> prefix/drive_c; others through dosdevices)."""
    d = child(prefix / "dosdevices", letter.lower() + ":") if (prefix / "dosdevices").is_dir() else None
    if d:
        try:
            return d.resolve()
        except OSError:
            pass
    return prefix / "drive_c" if letter.lower() == "c" else None


def in_prefix(prefix: Path) -> list[Path]:
    found = []
    drive_c = prefix / "drive_c"
    if not drive_c.is_dir():
        return found
    for rel in ("Program Files (x86)/World of Warcraft", "Program Files/World of Warcraft", "World of Warcraft",
                "Games/World of Warcraft", "Battle.net/World of Warcraft", "Blizzard/World of Warcraft"):
        found.append(forever_from(ci_path(drive_c, rel)))
    # The Battle.net app's list of installed games (a binary file; the install folders are plain text in it).
    db = ci_path(drive_c, "ProgramData/Battle.net/Agent/product.db")
    if db and db.is_file():
        try:
            text = db.read_bytes().decode("utf-8", errors="replace")
            for m in re.finditer(r'([A-Za-z]):[\\/]([^\x00-\x1f"<>|?*]{0,200}?World of Warcraft)', text):
                drive = prefix_drive(prefix, m.group(1))
                if drive:
                    found.append(forever_from(ci_path(drive, m.group(2))))
        except OSError:
            pass
    return [f for f in found if f]


def subdirs(folder: Path) -> list[Path]:
    try:
        return sorted(p for p in folder.iterdir() if p.is_dir())
    except OSError:
        return []


def lutris_prefixes() -> list[Path]:
    out = []
    for d in (HOME / ".config/lutris/games", HOME / ".local/share/lutris/games",
              HOME / ".var/app/net.lutris.Lutris/config/lutris/games",
              HOME / ".var/app/net.lutris.Lutris/data/lutris/games"):
        try:
            ymls = sorted(d.glob("*.yml"))
        except OSError:
            continue
        for y in ymls:
            try:
                text = y.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in re.finditer(r"^\s*(prefix|exe)\s*:\s*['\"]?(/[^'\"\n]+?)['\"]?\s*$", text, re.MULTILINE):
                p = Path(m.group(2))
                if m.group(1) == "exe":   # .../drive_c/... -> the prefix
                    parts = p.parts
                    low = [x.lower() for x in parts]
                    if "drive_c" not in low:
                        continue
                    p = Path(*parts[:low.index("drive_c")])
                out.append(p)
    return out


def steam_prefixes() -> list[Path]:
    roots = [HOME / ".steam/steam", HOME / ".steam/root", HOME / ".local/share/Steam",
             HOME / ".var/app/com.valvesoftware.Steam/.local/share/Steam", HOME / "snap/steam/common/.local/share/Steam"]
    libs = []
    for r in roots:
        libs.append(r)
        try:
            vdf = (r / "steamapps/libraryfolders.vdf").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        libs += [Path(m.replace("\\\\", "\\")) for m in re.findall(r'"path"\s+"([^"]+)"', vdf)]
    out = []
    for lib in libs:
        out += [d / "pfx" for d in subdirs(lib / "steamapps/compatdata")]
    return out


def find_forever() -> list[Path]:
    prefixes = []
    if os.environ.get("WINEPREFIX"):
        prefixes.append(Path(os.environ["WINEPREFIX"]))
    prefixes.append(HOME / ".wine")
    prefixes += lutris_prefixes()
    prefixes += subdirs(HOME / "Games")                                   # Lutris' default: ~/Games/battlenet
    prefixes += steam_prefixes()                                          # Battle.net added to Steam, run with Proton
    prefixes += subdirs(HOME / ".local/share/bottles/bottles")
    prefixes += subdirs(HOME / ".var/app/com.usebottles.bottles/data/bottles/bottles")
    prefixes += subdirs(HOME / ".local/share/wineprefixes") + subdirs(HOME / "Faugus")
    seen, found = set(), []
    for pre in prefixes:
        for f in in_prefix(pre):
            try:
                key = f.resolve()
            except OSError:
                key = f
            if key not in seen:
                seen.add(key)
                found.append(f)
    return found


def ask_for_folder() -> Path | None:
    if args.quiet or not sys.stdin.isatty():
        return None
    say("I couldn't find WoW Forever. Drag your World of Warcraft folder (the one with _forever_ inside) into this")
    say("window, then press Enter. Or type its path.")
    try:
        typed = input("> ").strip()
    except EOFError:
        return None
    if typed.startswith("file://"):
        typed = urllib.parse.unquote(urllib.parse.urlparse(typed).path)
    try:
        words = shlex.split(typed)            # terminals quote a dropped path, or escape its spaces
        typed = words[0] if len(words) == 1 else typed
    except ValueError:
        pass
    return forever_from(typed)


def resolve_addons() -> Path | None:
    if args.addons:
        return Path(args.addons).expanduser()
    cfg = read_config()
    if cfg.get("addons") and Path(cfg["addons"]).parent.is_dir():
        return Path(cfg["addons"])
    found = find_forever()
    forever = found[0] if found else None
    if len(found) > 1:
        say("Found more than one WoW Forever; using " + str(forever) + ".")
        say("Another one? Run this again with --addons \"<its _forever_/Interface/AddOns folder>\".")
    if not forever:
        forever = ask_for_folder()
    if not forever:
        return None
    interface = child(forever, "Interface") or forever / "Interface"
    return child(interface, "AddOns") or interface / "AddOns"


# --- Guards -------------------------------------------------------------------------------------------------------

def is_officer_pc(addons: Path) -> bool:
    # The officer PC gets UW_Guild from the bot's Update-Bot.cmd, a build ahead of the site: never touch it there.
    return (addons / ADDON / ".uw-local-tests.txt").exists()


def game_running() -> bool:
    proc = Path(args.proc_root)
    try:
        pids = [p for p in proc.iterdir() if p.name.isdigit()]
    except OSError:
        return False
    for p in pids:
        try:
            if GAME_RX.search((p / "cmdline").read_bytes()):
                return True
        except OSError:
            continue
    return False


# --- Files --------------------------------------------------------------------------------------------------------

def safe_remove(p: Path) -> None:
    # A link is removed itself, never followed (rmtree never descends into one).
    if p.is_symlink() or p.is_file():
        p.unlink()
    elif p.exists():
        shutil.rmtree(p)


def mark_toc(addons: Path, on: bool) -> None:
    # "## X-UW-Updater: <date>" in the installed TOC tells UW_Guild in game that this copy updates itself (Onboarding.lua
    # hasUpdater): its newer-version line then needs no link, and it never suggests the Windows setup.
    folder = addons / ADDON
    toc = folder / (ADDON + ".toc")
    if folder.is_symlink() or not toc.is_file():
        return
    mark = "## X-UW-Updater: " + datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    out, placed = [], not on
    for line in toc.read_text(encoding="utf-8").splitlines():
        if re.match(r"^\s*##\s*X-UW-Updater\s*:", line):
            continue
        out.append(line)
        if not placed and re.match(r"^\s*##\s*Version\s*:", line):
            out.append(mark)
            placed = True
    if not placed:
        out.insert(0, mark)
    toc.write_text("\n".join(out) + "\n", encoding="utf-8")


# --- Download and check -------------------------------------------------------------------------------------------

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):   # a redirect could leave https or the site: never follow one
        raise Problem("the site answered with a redirect")


def fetch(name: str) -> bytes:
    # From the site (https only), or from a folder (tests). No cache: a fresh deploy shows up at once.
    src = args.source
    if src.startswith("https://"):
        url = src.rstrip("/") + "/" + name + "?t=" + str(int(time.time() * 1000))
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "UW-Guild-Setup-Linux"})
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=60) as r:
                return r.read(MAX_ZIP + 1)
        except http.client.HTTPException as err:   # a cut-off download (IncompleteRead) isn't an OSError
            raise Problem("the download was cut off (" + type(err).__name__ + ")") from None
    if re.match(r"^[a-z]+://", src):
        raise Problem("only https:// sources are allowed, not " + src)
    return (Path(src) / name).read_bytes()


def rsa_key(xml: str) -> tuple[int, int]:
    def field(tag: str) -> int:
        m = re.search("<" + tag + ">([^<]+)</" + tag + ">", xml)
        if not m:
            raise Problem("the guild's key is unreadable")
        return int.from_bytes(base64.b64decode(m.group(1)), "big")
    return field("Modulus"), field("Exponent")


# DER prefix of a SHA-256 DigestInfo (RFC 8017, section 9.2, note 1).
SHA256_INFO = bytes.fromhex("3031300d060960864801650304020105000420")


def signature_ok(data: bytes, sig: bytes, xml: str) -> bool:
    """RSASSA-PKCS1-v1_5 verify with SHA-256 (RFC 8017, 8.2.2), as .NET's RSA VerifyData does on Windows."""
    n, e = rsa_key(xml)
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    s = int.from_bytes(sig, "big")
    if s >= n:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    want = b"\x00\x01" + b"\xff" * (k - 3 - len(SHA256_INFO) - 32) + b"\x00" + SHA256_INFO + hashlib.sha256(data).digest()
    return k >= len(SHA256_INFO) + 32 + 11 and hashlib.sha256(em).digest() == hashlib.sha256(want).digest()


def check_entries(z: zipfile.ZipFile, want: str) -> None:
    # Every entry must be a WoW addon file inside UW_Guild/, with no way out of it (zip-slip) and no links.
    total, toc = 0, None
    for info in z.infolist():
        n = info.filename.replace("\\", "/")
        if not n.startswith(ADDON + "/"):
            raise Problem("unexpected entry " + n)
        if re.search(r"(^|/)\.\.(/|$)", n) or ":" in n or n.startswith("/") or "\x00" in n:
            raise Problem("unsafe entry " + n)
        if stat.S_ISLNK(info.external_attr >> 16):
            raise Problem("a link in the zip: " + n)
        if n.endswith("/"):
            continue
        if os.path.splitext(n)[1].lower() not in SHIP:
            raise Problem("not an addon file: " + n)
        total += info.file_size
        if n == ADDON + "/" + ADDON + ".toc":
            toc = info
    if total > MAX_UNPACKED:
        raise Problem("the zip unpacks to more than 60 MB")
    if toc is None:
        raise Problem("no " + ADDON + ".toc in the zip")
    m = re.search(r"(?m)^\s*##\s*Version\s*:\s*(\S+)", z.read(toc).decode("utf-8", errors="replace"))
    if not m or m.group(1) != want:
        raise Problem("the zip's version doesn't match version.json")


def unpack(z: zipfile.ZipFile, dest: Path) -> None:
    root = os.path.realpath(dest) + os.sep
    for info in z.infolist():
        rel = info.filename.replace("\\", "/")[len(ADDON) + 1:]
        if not rel:
            continue
        target = os.path.realpath(os.path.join(dest, rel))
        if not target.startswith(root):
            raise Problem("unsafe entry " + info.filename)
        if rel.endswith("/"):
            os.makedirs(target, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with z.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)


def update(addons: Path) -> str:
    """'updated', 'current' or 'newer'. Raises Problem with a reason when it can't."""
    STATE.mkdir(parents=True, exist_ok=True)
    raw = fetch("version.json")
    if args.pubkey:
        try:
            sig = base64.b64decode(fetch("version.json.sig").decode("ascii").strip(), validate=True)
        except (Problem, OSError, ValueError):
            raise Problem("the site's version.json has no signature") from None
        try:
            ok = signature_ok(raw, sig, args.pubkey)
        except (Problem, ValueError):
            ok = False
        if not ok:
            raise Problem("the site's version.json signature doesn't match the guild's key")
    try:
        info = json.loads(raw.decode("utf-8-sig"))
    except ValueError:
        raise Problem("version.json is unreadable") from None
    want = str(info.get("version", ""))
    if not VERSION_RX.match(want):
        raise Problem("version.json has no valid version")
    if info.get("zip") != ADDON + ".zip":
        raise Problem("version.json names an unexpected file")
    sha = str(info.get("zip_sha256", "")).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise Problem("version.json has no valid SHA-256")
    size = info.get("zip_bytes")
    if not isinstance(size, int) or size <= 0 or size > MAX_ZIP:
        raise Problem("version.json has an unexpected size")

    here = addons / ADDON
    have = toc_version(here / (ADDON + ".toc"))
    if have == want:
        return "current"
    if version_key(have) > version_key(want):
        return "newer"

    data = fetch(ADDON + ".zip")
    if len(data) != size:
        raise Problem(f"the download is {len(data)} bytes, version.json says {size}")
    if hashlib.sha256(data).hexdigest() != sha:
        raise Problem("the download's SHA-256 doesn't match version.json")

    interface = addons.parent
    stage = interface / (ADDON + ".uw-new")        # outside AddOns, same disk: the swap is two renames
    backup = interface / (ADDON + ".uw-old")
    if here.is_symlink():
        raise Problem("AddOns/" + ADDON + " is a link to another folder; replace it by hand")
    for d in (stage, backup):
        safe_remove(d)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        check_entries(z, want)
        stage.mkdir(parents=True)
        unpack(z, stage)
    addons.mkdir(parents=True, exist_ok=True)
    moved = False
    try:
        if here.exists():
            os.rename(here, backup)
            moved = True
        os.rename(stage, here)
    except OSError as err:
        if moved and not here.exists():
            os.rename(backup, here)
        safe_remove(stage)
        raise Problem("couldn't swap the addon folder: " + str(err)) from None
    try:
        safe_remove(backup)
    except OSError:
        pass
    return "updated"


# --- Automatic updates --------------------------------------------------------------------------------------------

def quoted(p: Path) -> str:
    # One argument for systemd's ExecStart and a .desktop Exec line. The two escape differently, so a path that would
    # need escaping (rare: a quote, backslash, $, ` or % in a folder name) gets no timer rather than a wrong one.
    s = str(p)
    if re.search(r'["\\`$%\x00-\x1f]', s):
        raise Problem("the folder " + s + " has a character the timer can't take")
    return '"' + s + '"'


def python() -> Path:
    return Path("/usr/bin/python3") if Path("/usr/bin/python3").exists() else Path(sys.executable)


def systemctl(*a: str) -> None:
    r = subprocess.run(["systemctl", "--user"] + list(a), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       timeout=60, check=False)
    if r.returncode != 0:
        raise Problem("systemctl --user " + " ".join(a) + ": " + r.stderr.decode(errors="replace").strip())


def schedule() -> None:
    if not shutil.which("systemctl"):
        raise Problem("this Linux has no systemd")
    run = quoted(python()) + " -I " + quoted(SCRIPT_COPY)
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    (UNIT_DIR / (UNIT + ".service")).write_text(
        "[Unit]\nDescription=Keeps the UW_Guild WoW addon up to date from unbotheredwarlords.github.io. "
        "Installs only while WoW Forever is closed.\n\n"
        "[Service]\nType=oneshot\nExecStart=" + run + " --quiet\nNice=10\nIOSchedulingClass=idle\n"
        "TimeoutStartSec=10min\n", encoding="utf-8")
    # Hourly, at a random minute (spreads the guild's checks); Persistent runs a check missed while the PC was off.
    (UNIT_DIR / (UNIT + ".timer")).write_text(
        "[Unit]\nDescription=Check for a new UW_Guild once an hour\n\n"
        "[Timer]\nOnCalendar=hourly\nRandomizedDelaySec=50min\nPersistent=true\n\n"
        "[Install]\nWantedBy=timers.target\n", encoding="utf-8")
    systemctl("daemon-reload")
    systemctl("enable", "--now", UNIT + ".timer")
    MENU_DIR.mkdir(parents=True, exist_ok=True)
    for name, (title, what, extra) in MENU.items():
        (MENU_DIR / name).write_text(
            "[Desktop Entry]\nType=Application\nName=" + title + "\nComment=" + what + "\nExec=" + run
            + " --from-menu" + extra + "\nTerminal=true\nIcon=applications-games\nCategories=Game;\n", encoding="utf-8")


def unschedule() -> None:
    if shutil.which("systemctl"):
        try:
            systemctl("disable", "--now", UNIT + ".timer")
        except (Problem, OSError, subprocess.SubprocessError):
            pass
    for f in (UNIT_DIR / (UNIT + ".timer"), UNIT_DIR / (UNIT + ".service"), *(MENU_DIR / n for n in MENU)):
        try:
            f.unlink()
        except OSError:
            pass
    if shutil.which("systemctl"):
        try:
            systemctl("daemon-reload")
        except (Problem, OSError, subprocess.SubprocessError):
            pass


# --- Main ---------------------------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    global args, STATE, LOG, CONFIG, SCRIPT_COPY
    ap = argparse.ArgumentParser(description="Install UW_Guild for WoW Forever on Linux and keep it up to date.")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--no-schedule", action="store_true")
    ap.add_argument("--from-menu", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--test-run", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--addons", default="")
    ap.add_argument("--source", default=SOURCE, help=argparse.SUPPRESS)
    ap.add_argument("--state-dir", default="", help=argparse.SUPPRESS)
    ap.add_argument("--test-pubkey", default="", help=argparse.SUPPRESS)
    ap.add_argument("--proc-root", default="/proc", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    args.pubkey = PUBKEY
    if args.test_run:
        if not (args.addons and args.state_dir):
            print("--test-run needs --addons and --state-dir.")
            sys.exit(2)
        args.no_schedule = True
        if args.test_pubkey:          # a key the test made, never honoured outside --test-run
            args.pubkey = args.test_pubkey
    else:
        args.source, args.proc_root = SOURCE, "/proc"
    data_home = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local" / "share")
    STATE = Path(args.state_dir).expanduser() if args.state_dir else data_home / "uw-guild"
    LOG, CONFIG, SCRIPT_COPY = STATE / "update.log", STATE / "config.json", STATE / "uw-update.py"

    say("UW Guild addon setup")
    say()
    if not args.test_run and hasattr(os, "geteuid") and os.geteuid() == 0:
        fail("Run this without sudo: it only changes your own files (WoW's AddOns folder and your own timer).")
    if args.remove:
        cfg = read_config()
        if cfg.get("addons"):
            try:
                mark_toc(Path(cfg["addons"]), False)
            except OSError:
                pass
        if not args.test_run:
            unschedule()
        for f in (SCRIPT_COPY, CONFIG, STATE / "version.json"):
            try:
                f.unlink()
            except OSError:
                pass
        log("automatic updates removed")
        say("Automatic updates are off. The addon stays in WoW; to remove it too, delete the UW_Guild folder in AddOns.")
        done(0)

    found = resolve_addons()
    if not found:
        fail("Couldn't find WoW Forever. Install it with the Battle.net app first, or whisper an officer in game.")
    addons = Path(os.path.abspath(str(found)))
    if addons.name.lower() != "addons" or addons.parent.name.lower() != "interface":
        fail(str(addons) + " isn't an Interface/AddOns folder.")
    if not args.test_run and is_officer_pc(addons):
        log("officer PC: skipped (Update-Bot.cmd installs UW_Guild here)")
        say("This PC gets UW_Guild from the bot's Update-Bot.cmd. Nothing changed.")
        done(0)
    say("WoW Forever: " + str(addons.parent.parent))

    if game_running():
        if args.quiet:
            log("WoW is running: checked later")
            sys.exit(0)
        fail("WoW Forever is open. Close it first (an update while it runs would load half of it), then run this again.")

    try:
        result = update(addons)
    except (Problem, OSError, zipfile.BadZipFile) as err:
        fail("Couldn't update UW_Guild: " + str(err) + ". Nothing changed; it tries again within the hour.")
    now = toc_version(addons / ADDON / (ADDON + ".toc"))
    log(f"{result} {now} ({addons})")
    say({"updated": "UW_Guild " + now + " is installed.",
         "current": "UW_Guild " + now + " is already the newest.",
         "newer": "This PC has a newer UW_Guild (" + now + ") than the site. Nothing changed."}[result])

    # Automatic updates, on by default (Louis, 8 Nov 2026: one button, zero clicks for updates), said in plain words
    # before the timer is set up. Only from the downloaded setup: the menu's "Update now" runs the installed copy and
    # leaves the timer as it is. The timer runs a static copy of this file made here; nothing ever replaces it from the
    # network, so a new setup is always a new download someone chose to run.
    me = Path(os.path.abspath(sys.argv[0])) if sys.argv and sys.argv[0] else None
    from_copy = me is not None and me == Path(os.path.abspath(str(SCRIPT_COPY)))
    cfg = read_config()
    auto = bool(cfg.get("auto"))
    if not args.pubkey and not args.no_schedule and not args.quiet and not from_copy:
        say("To update later, download and run this setup again.")
    elif not args.no_schedule and not args.quiet and not from_copy and me is not None:
        say()
        say("Turning on automatic updates: a timer named \"" + UNIT + "\" checks unbotheredwarlords.github.io")
        say("once an hour and, while WoW is closed, replaces only the UW_Guild addon folder with the newest one.")
        say("Stop it any time: your app menu, Stop UW Guild addon automatic updates.")
        try:
            STATE.mkdir(parents=True, exist_ok=True)
            SCRIPT_COPY.write_bytes(me.read_bytes())
            schedule()
            CONFIG.write_text(json.dumps({"addons": str(addons), "auto": True}, indent=2) + "\n", encoding="utf-8")
            auto = True
            log("automatic updates on")
            say("Automatic updates: on.")
        except (Problem, OSError, subprocess.SubprocessError) as err:
            log("schedule failed: " + str(err))
            say("The addon is installed, but automatic updates couldn't be set up (" + str(err) + ").")
            say("Run this setup again later to update.")
    if auto and result != "newer":
        try:
            mark_toc(addons, True)
        except OSError as err:
            log("couldn't mark the TOC: " + str(err))

    if result == "updated":
        say()
        say("Next: start WoW. A welcome window opens when you log in.")
        say("Not there? On the character screen, click AddOns, tick UW Guild (and Load out of date AddOns if it asks).")
    done(0)


if __name__ == "__main__":
    main()
