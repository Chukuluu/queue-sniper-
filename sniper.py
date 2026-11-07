#!/usr/bin/env python3
"""
Queue Sniper
------------
Watches configured Discord channels and automatically clicks the
"Enter Waitlist" / "Join Queue" style button the moment it appears.
Also plays a loud alarm (and optionally speaks) so you actually notice.

This uses a Discord *user* account (self-bot). That is against Discord's
Terms of Service and can get your account banned. Use an alt account.

Commands:
    python sniper.py --setup        add/remove watched servers and channels
    python sniper.py --list         list servers/channels and IDs
    python sniper.py                run the sniper
    python sniper.py --dry-run      alarm only, never click
    python sniper.py --debug        print every button seen in watched channels
    python sniper.py --test-alarm   test the sound/voice
    python sniper.py --self-test    verify component matching logic
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import ctypes
import json
import math
import os
import queue
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

try:
    import discord
except ImportError:
    print("discord.py-self is not installed.")
    print("Run install.bat first (or: pip install -r requirements.txt)")
    raise SystemExit(1)

BASE_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"
ENV_PATH = BASE_DIR / ".env"
LOG_DIR = BASE_DIR / "logs"
ALARM_WAV = BASE_DIR / "alarm.wav"

DEFAULT_CONFIG = {
    # Simple mode: the server + channel that contains the waitlist message.
    "server_id": "",
    "channel_id": "",
    # server_id -> friendly name (optional; lets the sniper sanity-check where
    # each watched channel lives when you watch channels across servers).
    "watch_servers": {},
    # channel_id -> friendly name (optional; for watching more than one channel).
    "watch_channels": {},
    # Also watch messages inside threads whose parent channel is watched.
    "watch_threads": True,
    # Buttons are matched against "<label> <custom_id>".
    "button_patterns": [
        r"enter\s+(the\s+)?wait\s*-?\s*list",
        r"join\s+(the\s+)?wait\s*-?\s*list",
        r"\bwait\s*-?\s*list\b",
        r"enter\s+(the\s+)?queue",
        r"join\s+(the\s+)?queue",
        r"queue\s+(join|enter)",
        r"click\s+(here\s+)?to\s+(join|enter)",
        r"join\s+now",
    ],
    # If any of these match, the button is ignored (prevents clicking "Leave").
    "deny_patterns": [
        r"\bleave\b",
        r"\bexit\b",
        r"\bquit\b",
        r"\bcancel\b",
        r"\bremove\b",
        r"\bview\b",
        r"\bcheck\b",
        r"\b(wait\s*-?\s*list|queue)\s*(info|status|stats)\b",
    ],
    "match_custom_id": True,
    # Minimum seconds between clicks in the same channel.
    "click_cooldown_seconds": 0.25,
    # Allow clicking the same button on the same message again after this many
    # seconds. 0 = only re-click once the button has been seen disabled and
    # re-enabled again (the usual "queue closed then reopened" edit).
    "rearm_after_seconds": 0,
    "retry_attempts": 2,
    "retry_delay_ms": 120,
    # Alert only, never press the button.
    "dry_run": False,
    "debug_components": False,
    "raw_edit_fallback": True,
    # Keep the HTTPS/HTTP3 connection to discord.com warm. A cold request pays
    # a TCP+TLS(QUIC) handshake (~40-150 ms) *before* the click is sent. 0 turns
    # the warm-up requests off.
    "keepalive_seconds": 20,
    # Raise the process priority so the click is handled promptly even while a
    # game (or anything CPU heavy) is running.
    "boost_process": True,
    "startup_sound": True,
    "alarm": {
        "enabled": True,
        "repeat": 3,
        "gap_seconds": 0.25,
        "voice": True,
        "voice_text": "Queue open. Go!",
        "flash_taskbar": True,
    },
    "log_to_file": True,
}

ANSI = {
    "RESET": "\033[0m",
    "DEBUG": "\033[90m",
    "INFO": "\033[36m",
    "OK": "\033[92m",
    "HIT": "\033[95m",
    "WARN": "\033[93m",
    "ERROR": "\033[91m",
}

_alarm_lock = threading.Lock()


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------

def enable_ansi() -> None:
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def measure_clock_offset(timeout: float = 1.0) -> float:
    """Return server_time - local_time in seconds using SNTP.

    Discord snowflake timestamps come from Discord's clock. If the PC clock is
    off, every message looks older/newer than it really is. 0.0 means unknown.
    """
    try:
        import socket

        packet = b"\x1b" + 47 * b"\0"
        best: Optional[tuple[float, float]] = None
        for host in ("time.cloudflare.com", "pool.ntp.org"):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.settimeout(timeout)
                    t1 = time.time()
                    sock.sendto(packet, (host, 123))
                    data, _ = sock.recvfrom(1024)
                    t4 = time.time()
                if len(data) < 48:
                    continue
                ints = struct.unpack("!12I", data[:48])
                t2 = ints[8] + ints[9] / 2**32 - 2208988800
                t3 = ints[10] + ints[11] / 2**32 - 2208988800
                offset = ((t2 - t1) + (t3 - t4)) / 2
                delay = (t4 - t1) - (t3 - t2)
                if best is None or delay < best[0]:
                    best = (delay, offset)
            except Exception:
                continue
        return best[1] if best is not None else 0.0
    except Exception:
        return 0.0


def tune_process(cfg: dict) -> None:
    """Small Windows tweaks that help under load / for short sleeps."""
    if os.name != "nt" or not cfg.get("boost_process", True):
        return
    try:
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        # ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
        ctypes.windll.kernel32.SetPriorityClass(handle, 0x00008000)
    except Exception:
        pass
    try:
        # 1 ms timer resolution so retry sleeps are not rounded up to ~15.6 ms.
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass


def safe_print(text: str, stream=None) -> None:
    """Print without crashing on emoji when stdout uses a legacy code page."""
    stream = stream if stream is not None else sys.stdout
    try:
        print(text, file=stream, flush=True)
    except Exception:
        try:
            encoding = getattr(stream, "encoding", None) or "ascii"
            fixed = text.encode(encoding, "replace").decode(encoding, "replace")
            print(fixed, file=stream, flush=True)
        except Exception:
            pass


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> dict:
    if path.exists():
        try:
            user_cfg = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            print(f"Could not read {path}: {exc}")
            print("Fix the JSON or delete the file to start over.")
            raise SystemExit(1)
    else:
        user_cfg = {}
    return deep_merge(DEFAULT_CONFIG, user_cfg)


def save_config(cfg: dict, path: Path = DEFAULT_CONFIG_PATH) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_env(path: Path = ENV_PATH) -> dict:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def save_env(token: str, path: Path = ENV_PATH) -> None:
    path.write_text(
        "# Discord user token. NEVER share this file.\n"
        f"DISCORD_TOKEN={token}\n",
        encoding="utf-8",
    )


def _clean_token(raw: str) -> str:
    """Strip quotes, bracketed-paste escapes and invisible characters."""
    value = (raw or "").strip().strip('"').strip("'")
    value = re.sub(r"\x1b\[20[01]~", "", value)
    value = "".join(ch for ch in value if ch.isprintable())
    return value.strip()


def _looks_like_token(value: str) -> bool:
    return 20 <= len(value) <= 300 and not any(ch.isspace() for ch in value)


def _plausible_discord_token(value: str) -> bool:
    # User/bot tokens look like base64url.base64url.signature
    return len(value) >= 50 and value.count(".") >= 2 and _looks_like_token(value)


def _clipboard_token() -> str:
    if os.name != "nt":
        return ""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        return _clean_token(result.stdout)
    except Exception:
        return ""


def get_token(interactive: bool = True) -> Optional[str]:
    token = _clean_token(
        os.environ.get("DISCORD_TOKEN") or load_env().get("DISCORD_TOKEN") or ""
    )
    if token:
        return token
    if not interactive:
        return None

    print()
    print("No token found. How to get your Discord token:")
    print("  1. Open https://discord.com/app in Chrome/Edge and log in.")
    print("  2. Press F12 -> Application -> Local Storage -> https://discord.com")
    print("  3. Copy the value of the 'token' key (without quotes).")
    print("     Alt route: Network tab -> any /api request -> Headers -> authorization")
    print()
    print("  WARNING: this token is full access to your account.")
    print("           Use an alt account, and never share the token.")
    print()

    for attempt in range(1, 4):
        print()
        # Visible input: hidden input + paste is unreliable in some consoles.
        token = _clean_token(input("Paste the token here, then press Enter: "))
        if not token:
            clip = _clipboard_token()
            if _plausible_discord_token(clip):
                token = clip
                print("  (Nothing was typed, so I used the token on your clipboard.)")
        if not token:
            print("  Nothing was entered. Copy the token, then try again.")
            print("  Ctrl+V or right-click pastes in this window.")
            continue
        if not _plausible_discord_token(token):
            print(f"  That does not look like a Discord token (length {len(token)}).")
            if attempt < 3:
                print("  Try again.")
                continue
            print("  Saving it anyway.")
        save_env(token)
        print(f"  Saved token to {ENV_PATH}")
        return token

    return None


def normalize_watch(raw) -> dict[int, str]:
    if isinstance(raw, dict):
        out = {}
        for key, value in raw.items():
            try:
                out[int(key)] = str(value)
            except (TypeError, ValueError):
                continue
        return out
    if isinstance(raw, list):
        out = {}
        for key in raw:
            try:
                out[int(key)] = ""
            except (TypeError, ValueError):
                continue
        return out
    return {}


def parse_indices(text: str, count: int) -> list[int]:
    text = text.strip().lower().replace(" ", "")
    if text in ("all", "*"):
        return list(range(count))
    result: set[int] = set()
    for part in text.split(","):
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            for i in range(int(start), int(end) + 1):
                result.add(i - 1)
        else:
            result.add(int(part) - 1)
    return sorted(i for i in result if 0 <= i < count)


class Logger:
    """Console logging stays synchronous; file writes happen on a worker thread.

    The file write used to open/write/close the log for every line, which put
    disk I/O directly in front of the queue-click POST. Writing is now off the
    event loop so a slow disk can never delay a click.
    """

    def __init__(self, cfg: dict):
        self.to_file = bool(cfg.get("log_to_file", True))
        self.path: Optional[Path] = None
        self._queue: Optional[queue.SimpleQueue] = None
        self._thread: Optional[threading.Thread] = None
        if self.to_file:
            try:
                LOG_DIR.mkdir(exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d")
                self.path = LOG_DIR / f"sniper-{stamp}.log"
            except Exception:
                self.path = None
        if self.path is not None:
            self._queue = queue.SimpleQueue()
            self._thread = threading.Thread(
                target=self._write_loop, name="log-writer", daemon=True
            )
            self._thread.start()
            atexit.register(self.close)

    def __call__(self, message: str, level: str = "INFO") -> None:
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{stamp}] {level:<7} {message}"
        color = ANSI.get(level.upper(), "")
        reset = ANSI["RESET"] if color else ""
        safe_print(f"{color}{line}{reset}", sys.stdout)
        if self._queue is not None:
            self._queue.put(line)

    def _write_loop(self) -> None:
        q = self._queue
        while True:
            try:
                line = q.get()
            except Exception:
                return
            if line is None:
                return
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except Exception:
                pass

    def close(self) -> None:
        q = self._queue
        thread = self._thread
        self._queue = None
        self._thread = None
        if q is not None:
            q.put(None)
        if thread is not None:
            thread.join(timeout=1.5)


# --------------------------------------------------------------------------
# Sound / notification
# --------------------------------------------------------------------------

def ensure_alarm_wav(path: Path = ALARM_WAV) -> Path:
    rate = 44100

    def build(target: Path) -> Path:
        frames = bytearray()

        def tone(freq: float, duration: float, amp: float = 0.6) -> None:
            total = int(rate * duration)
            fade = max(1, int(rate * 0.012))
            for i in range(total):
                if i < fade:
                    env = i / fade
                elif i > total - fade:
                    env = (total - i) / fade
                else:
                    env = 1.0
                sample = int(32767 * amp * env * math.sin(2 * math.pi * freq * i / rate))
                frames.extend(struct.pack("<h", sample))
            frames.extend(b"\x00\x00" * int(rate * 0.04))

        tone(880, 0.16)
        tone(1175, 0.16)
        tone(1568, 0.34)

        with wave.open(str(target), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(bytes(frames))
        return target

    candidates = [path, Path(tempfile.gettempdir()) / "queue-sniper-alarm.wav"]
    last_error: Optional[Exception] = None
    for target in candidates:
        try:
            if target.exists() and target.stat().st_size > 1000:
                return target
            target.parent.mkdir(parents=True, exist_ok=True)
            return build(target)
        except OSError as exc:
            # e.g. the exe lives somewhere read-only; fall back to %TEMP%.
            last_error = exc
    raise last_error if last_error else OSError("could not create alarm.wav")


def speak(text: str) -> None:
    if os.name != "nt" or not text:
        return
    # Strip non-ASCII (emoji channel names) so the PowerShell command cannot
    # break on code-page conversion.
    text = "".join(ch if ord(ch) < 128 else " " for ch in text)
    text = " ".join(text.split())[:220].replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.Volume = 100; $s.Rate = 1; "
        f"$s.Speak('{text}')"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
            timeout=15,
        )
    except Exception:
        pass


def flash_taskbar() -> None:
    if os.name != "nt":
        return
    try:
        from ctypes import wintypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("hwnd", wintypes.HWND),
                ("dwFlags", wintypes.DWORD),
                ("uCount", wintypes.UINT),
                ("dwTimeout", wintypes.DWORD),
            ]

        # FLASHW_ALL | FLASHW_TIMERNOFG
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x00000003 | 0x0000000C, 6, 0)
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


def play_alarm(text: str, cfg: dict, logger: Logger) -> None:
    alarm = cfg.get("alarm", {})
    if not alarm.get("enabled", True):
        return

    def run() -> None:
        try:
            # Queue alarms instead of dropping them, so two queues opening at
            # the same time both make noise (one after the other).
            with _alarm_lock:
                repeats = max(1, int(alarm.get("repeat", 3)))
                gap = max(0.0, float(alarm.get("gap_seconds", 0.25)))
                if os.name == "nt":
                    import winsound

                    wav = ensure_alarm_wav()
                    for _ in range(repeats):
                        winsound.PlaySound(str(wav), winsound.SND_FILENAME)
                        time.sleep(gap)
                    if alarm.get("voice", True):
                        speak(text or alarm.get("voice_text", "Queue open"))
                    if alarm.get("flash_taskbar", True):
                        flash_taskbar()
                else:
                    for _ in range(repeats):
                        sys.stdout.write("\a")
                        sys.stdout.flush()
                        time.sleep(0.35)
        except Exception as exc:
            logger(f"Alarm failed: {exc}", "WARN")

    threading.Thread(target=run, daemon=True).start()


def play_error() -> None:
    def run() -> None:
        try:
            if os.name == "nt":
                import winsound

                winsound.Beep(320, 220)
                time.sleep(0.10)
                winsound.Beep(220, 320)
            else:
                sys.stdout.write("\a")
                sys.stdout.flush()
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


def play_startup() -> None:
    def run() -> None:
        try:
            if os.name == "nt":
                import winsound

                winsound.Beep(660, 110)
                time.sleep(0.08)
                winsound.Beep(990, 140)
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


# --------------------------------------------------------------------------
# Component matching
# --------------------------------------------------------------------------

def iter_buttons(components: Iterable) -> Iterator["discord.Button"]:
    """Recursively yield buttons from action rows, containers, sections, etc."""
    def walk(component) -> Iterator["discord.Button"]:
        if isinstance(component, discord.Button):
            yield component
            return
        for child in getattr(component, "children", None) or []:
            yield from walk(child)
        accessory = getattr(component, "accessory", None)
        if accessory is not None:
            yield from walk(accessory)
        inner = getattr(component, "component", None)
        if inner is not None:
            yield from walk(inner)

    for component in components or []:
        yield from walk(component)


class ButtonMatcher:
    def __init__(self, cfg: dict):
        self.allow = [re.compile(p, re.IGNORECASE) for p in cfg.get("button_patterns", [])]
        self.deny = [re.compile(p, re.IGNORECASE) for p in cfg.get("deny_patterns", [])]
        self.use_custom_id = bool(cfg.get("match_custom_id", True))

    def matches(self, button: "discord.Button") -> bool:
        if getattr(button, "disabled", False):
            return False
        if getattr(button, "url", None):
            return False
        if getattr(button, "sku_id", None) is not None:
            return False
        custom_id = getattr(button, "custom_id", None) or ""
        if not custom_id:
            return False
        label = getattr(button, "label", None) or ""
        raw = f"{label} {custom_id}" if self.use_custom_id else label
        # custom_ids like join_waitlist should match "join waitlist" patterns
        haystack = raw.replace("_", " ").replace("-", " ")
        if any(pattern.search(haystack) for pattern in self.deny):
            return False
        return any(pattern.search(haystack) for pattern in self.allow)


# --------------------------------------------------------------------------
# Target resolution / interactive setup
# --------------------------------------------------------------------------

def clean_id(raw) -> str:
    """Keep digits only; Discord IDs are 15-22 digit snowflakes."""
    digits = re.sub(r"[^0-9]", "", str(raw or ""))
    return digits if 15 <= len(digits) <= 22 else ""


def parse_channel_ref(raw: str) -> tuple[str, str]:
    """Parse a channel link or a bare channel ID into (server_id, channel_id)."""
    text = (raw or "").strip()
    match = re.search(r"discord(?:app)?\.com/channels/(\d+)/(\d+)", text)
    if match:
        return clean_id(match.group(1)), clean_id(match.group(2))
    return "", clean_id(text)


def resolve_watch(cfg: dict) -> dict[int, str]:
    """channel_id is the simple mode; watch_channels stays for multi-channel."""
    watch = normalize_watch(cfg.get("watch_channels"))
    channel_id = clean_id(cfg.get("channel_id"))
    if channel_id:
        watch[int(channel_id)] = watch.get(int(channel_id), "")
    return watch


def resolve_server_id(cfg: dict) -> int:
    try:
        return int(clean_id(cfg.get("server_id")) or 0)
    except (TypeError, ValueError):
        return 0


def resolve_servers(cfg: dict) -> dict[int, str]:
    """All servers the watched channels may live in (single + multi mode)."""
    servers = normalize_watch(cfg.get("watch_servers"))
    server_id = resolve_server_id(cfg)
    if server_id:
        servers.setdefault(server_id, "")
    return servers


def prompt_targets(cfg: dict, config_path: Path, logger: Logger) -> bool:
    print("\n=== Queue Sniper Setup ===\n")
    print("In Discord: Settings > Advanced > Developer Mode (turn it ON), then:")
    print("  right-click a server           -> Copy Server ID")
    print("  right-click the waitlist channel -> Copy Channel ID")
    print("You can paste with Ctrl+V or right-click, or paste a channel link.")
    print("Tip: close any other QueueSniper window before changing the list.\n")

    # Move any single-target server_id/channel_id into the multi lists so the
    # menu shows and edits one consistent list.
    watch = resolve_watch(cfg)
    servers = resolve_servers(cfg)
    cfg["watch_channels"] = {str(cid): name for cid, name in watch.items()}
    cfg["watch_servers"] = {str(sid): name for sid, name in servers.items()}
    cfg["server_id"] = ""
    cfg["channel_id"] = ""

    def show_targets() -> None:
        entries = cfg["watch_channels"]
        if not entries:
            print("  (no channels configured yet)")
            return
        print(f"  {len(entries)} channel(s) watched:")
        for index, (channel_id, friendly) in enumerate(entries.items(), 1):
            label = f"  {friendly}" if friendly else ""
            print(f"    {index}) {channel_id}{label}")

    def add_target() -> None:
        print("\nAdd a channel - paste a channel link, or type the Server ID.")
        server_id = ""
        channel_id = ""
        raw = input("Server ID or channel link (blank to cancel): ").strip()
        if not raw:
            return
        if re.search(r"discord(?:app)?\.com/channels/", raw, re.IGNORECASE):
            server_id, channel_id = parse_channel_ref(raw)
        else:
            server_id = clean_id(raw)
            if not server_id:
                print("  That is not a valid server ID or channel link.")
                return

        if not channel_id:
            raw = input("Channel ID or channel link (blank to cancel): ").strip()
            if not raw:
                return
            link_server, channel_id = parse_channel_ref(raw)
            if link_server and not server_id:
                server_id = link_server
            if not channel_id:
                print("  That is not a valid channel ID or channel link.")
                return

        if channel_id in cfg["watch_channels"]:
            print(f"  Channel {channel_id} is already in the list.")
            return
        cfg["watch_channels"][channel_id] = ""
        if server_id:
            cfg["watch_servers"].setdefault(server_id, "")
        print(f"  Added channel {channel_id}.")
        if server_id:
            print(f"  Server {server_id}.")
        print("  (The real server/channel name is shown when the sniper connects.)")

    def remove_target() -> None:
        entries = list(cfg["watch_channels"])
        if not entries:
            print("  Nothing to remove.")
            return
        show_targets()
        raw = input("Number to remove (blank to cancel): ").strip()
        if not raw:
            return
        try:
            index = int(raw)
        except ValueError:
            print("  Enter the number shown next to the channel.")
            return
        if not 1 <= index <= len(entries):
            print(f"  Enter a number from 1 to {len(entries)}.")
            return
        channel_id = entries[index - 1]
        del cfg["watch_channels"][channel_id]
        print(f"  Removed channel {channel_id}.")

    while True:
        print("\nCurrently watching:")
        show_targets()
        print("\n  [1] Add a server + channel")
        print("  [2] Remove a channel")
        print("  [3] Clear the whole list")
        print("  [4] Done - save and start")
        choice = (input("Choice [4]: ") or "4").strip().lower()

        if choice in ("1", "a", "add"):
            add_target()
        elif choice in ("2", "r", "remove"):
            remove_target()
        elif choice in ("3", "c", "clear"):
            if not cfg["watch_channels"]:
                print("  The list is already empty.")
            elif input("Remove all channels? [y/N]: ").strip().lower().startswith("y"):
                cfg["watch_channels"] = {}
                cfg["watch_servers"] = {}
                print("  Cleared the whole list.")
        elif choice in ("4", "d", "done", ""):
            if not cfg["watch_channels"]:
                print("\nAdd at least one channel first.")
                continue
            break
        else:
            print("  Enter 1, 2, 3 or 4.")

    default = "2" if cfg.get("dry_run") else "1"
    print("\nMode:")
    print("  [1] auto-click + alarm")
    print("  [2] alarm only (dry run)")
    mode = (input(f"Choice [{default}]: ") or default).strip()

    cfg["dry_run"] = mode.startswith("2")
    save_config(cfg, config_path)

    logger(
        f"Saved {len(cfg['watch_channels'])} channel(s) to {config_path}",
        "OK",
    )
    logger(f"Mode: {'DRY RUN (no clicking)' if cfg['dry_run'] else 'LIVE (auto-click)'}", "OK")
    return True


def ask_setup_or_start(cfg: dict, timeout: float = 4.0) -> bool:
    """Offer setup when the exe is double-clicked; start automatically otherwise.

    Returns True if the user asked to change the watched servers/channels.
    """
    if os.name != "nt" or "--no-pause" in sys.argv:
        return False
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return False
    except Exception:
        return False

    watch = resolve_watch(cfg)
    print("\n=== Queue Sniper ===")
    if watch:
        print(f"Watching {len(watch)} channel(s).")
    else:
        print("No channels configured yet.")
    print("Press  S  for setup (add/remove servers and channels).")
    print(f"Starting automatically in {int(timeout)} seconds...")

    try:
        import msvcrt
    except Exception:
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if msvcrt.kbhit():
            key = msvcrt.getwch()
            if key.lower() == "s":
                print("\nOpening setup...")
                return True
            print("\nStarting...")
            return False
        time.sleep(0.05)
    print("\nStarting...")
    return False


# --------------------------------------------------------------------------
# List client (--list)
# --------------------------------------------------------------------------

class ListClient(discord.Client):
    def __init__(self, cfg: dict, logger: Logger):
        super().__init__(guild_subscriptions=True)
        self.cfg = cfg
        self.logger = logger

    async def on_ready(self) -> None:
        try:
            self.print_list()
        except Exception as exc:
            self.logger(f"List failed: {exc!r}", "ERROR")
        finally:
            await self.close()

    def channels_of(self, guild: "discord.Guild") -> list:
        channels = sorted(guild.text_channels, key=lambda c: (c.position, c.name))
        threads = sorted(guild.threads, key=lambda c: c.name)
        seen = set()
        result = []
        for channel in channels + threads:
            if channel.id not in seen:
                seen.add(channel.id)
                result.append(channel)
        return result

    def print_list(self) -> None:
        watch = set(resolve_watch(self.cfg))
        safe_print("\nCopy the IDs you need (right-click in Discord also works):\n")
        for guild in sorted(self.guilds, key=lambda g: g.name.lower()):
            safe_print(f"SERVER  {guild.name}  ({guild.id})")
            for channel in self.channels_of(guild):
                marker = "  <== WATCHING" if channel.id in watch else ""
                safe_print(f"   #{channel.name:<32} {channel.id}{marker}")
            safe_print("")


# --------------------------------------------------------------------------
# Main sniper client
# --------------------------------------------------------------------------

class QueueSniper(discord.Client):
    def __init__(self, cfg: dict, logger: Logger):
        super().__init__(guild_subscriptions=True)
        self.cfg = cfg
        self.logger = logger
        self.watch = resolve_watch(cfg)
        self.servers = resolve_servers(cfg)
        self.matcher = ButtonMatcher(cfg)
        # (message_id, custom_id) -> monotonic time of the click.
        self._clicked: dict[tuple[int, str], float] = {}
        self._last_click: dict[int, float] = {}
        self.stats = {"messages": 0, "detected": 0, "clicks": 0, "errors": 0}
        self._started = time.time()
        self._warm_task: Optional[asyncio.Future] = None
        # Server clock minus PC clock (seconds), used to correct message ages.
        self.clock_offset = measure_clock_offset()
        # message_id -> perf_counter when the gateway dispatched it to us.
        self._event_at: dict[int, float] = {}

    def dispatch(self, event, /, *args, **kwargs):
        """Remember when a message event reached us (before handlers run)."""
        message_id = None
        if event == "message" and args:
            message_id = getattr(args[0], "id", None)
        elif event == "message_edit" and len(args) >= 2:
            message_id = getattr(args[-1], "id", None)
        elif event == "raw_message_edit" and args:
            message_id = getattr(args[0], "message_id", None)
        if message_id is not None:
            self._event_at[message_id] = time.perf_counter()
            if len(self._event_at) > 512:
                for stale in list(self._event_at)[:256]:
                    self._event_at.pop(stale, None)
        return super().dispatch(event, *args, **kwargs)

    def watched(self, channel) -> bool:
        if channel is None:
            return False
        if channel.id in self.watch:
            return True
        if self.cfg.get("watch_threads", True):
            parent_id = getattr(channel, "parent_id", None)
            if parent_id in self.watch:
                return True
        return False

    def cached_message(self, message_id: int):
        try:
            for message in self.cached_messages:
                if message.id == message_id:
                    return message
        except Exception:
            pass
        return None

    async def on_ready(self) -> None:
        self.logger(f"Logged in as {self.user} ({self.user.id})", "OK")
        if self.cfg.get("dry_run"):
            self.logger("DRY RUN: buttons will NOT be clicked (alarm only)", "WARN")
        else:
            self.logger("LIVE: buttons will be auto-clicked", "HIT")

        offset = self.clock_offset
        if abs(offset) > 0.25:
            direction = "behind" if offset > 0 else "ahead of"
            self.logger(
                f"PC clock is {abs(offset):.2f}s {direction} real time. "
                "Timing is auto-corrected; sync Windows time for accurate logs.",
                "WARN",
            )

        if not self.watch:
            self.logger("No channel configured! Run: QueueSniper.exe --setup", "ERROR")

        if self.servers:
            for server_id, friendly in self.servers.items():
                guild = self.get_guild(server_id)
                if guild is None:
                    label = f" ({friendly})" if friendly else ""
                    self.logger(
                        f"Server {server_id}{label} not found - is the account in it?",
                        "WARN",
                    )
                else:
                    self.logger(f"Server: {guild.name} ({guild.id})", "INFO")

        for channel_id, name in self.watch.items():
            channel = self.get_channel(channel_id)
            if channel is None:
                self.logger(
                    f"Cannot see channel {channel_id} - check the channel ID and "
                    "that the account is in that server",
                    "WARN",
                )
            else:
                guild = getattr(channel, "guild", None)
                if self.servers and guild is not None and guild.id not in self.servers:
                    self.logger(
                        f"Channel {channel_id} is in {guild.name} ({guild.id}), "
                        "which is not in watch_servers",
                        "WARN",
                    )
                self.logger(
                    f"Watching #{getattr(channel, 'name', name or channel_id)} ({channel_id})",
                    "INFO",
                )

        self.start_keepalive()
        self.logger("Listening for queue messages... (Ctrl+C to stop)", "INFO")
        if self.cfg.get("startup_sound", True):
            play_startup()

    def start_keepalive(self) -> None:
        """Keep the API connection (HTTP/3) warm so a click skips the handshake."""
        try:
            interval = float(self.cfg.get("keepalive_seconds", 20) or 0)
        except (TypeError, ValueError):
            interval = 0.0
        if interval <= 0:
            return
        if self._warm_task is not None and not self._warm_task.done():
            return
        self._warm_task = asyncio.ensure_future(self._keepalive_loop(interval))
        self.logger(f"Keeping the API connection warm (every {interval:g}s)", "DEBUG")

    async def _keepalive_loop(self, interval: float) -> None:
        # Small initial delay: login already just used the connection.
        await asyncio.sleep(min(interval, 5.0))
        while not self.is_closed():
            try:
                # Use an authenticated route on purpose: /gateway is served by a
                # nearby edge node, while the interaction click goes to Discord's
                # origin. Warming the origin keeps that TLS/QUIC session open, so
                # the first click after idle does not pay a handshake penalty.
                await self.http.get_me(with_analytics_token=False)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger(f"Keep-alive request failed: {exc!r}", "DEBUG")
            await asyncio.sleep(interval)

    async def close(self) -> None:
        task = self._warm_task
        self._warm_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        await super().close()

    async def on_disconnect(self) -> None:
        self.logger("Gateway disconnected - reconnecting...", "WARN")

    async def on_resumed(self) -> None:
        self.logger("Gateway resumed", "OK")

    async def on_message(self, message: "discord.Message") -> None:
        if not self.watched(getattr(message, "channel", None)):
            return
        await self.scan(message)

    async def on_message_edit(self, before: "discord.Message", after: "discord.Message") -> None:
        if not self.watched(getattr(after, "channel", None)):
            return
        # `after` can be a partial message on edits. Prefer the cached copy,
        # which has the full author/application_id and the new components.
        message = self.cached_message(after.id) or after
        await self.scan(message)

    async def on_raw_message_edit(self, payload) -> None:
        if not self.cfg.get("raw_edit_fallback", True):
            return
        if getattr(payload, "cached_message", None) is not None:
            return  # on_message_edit handles it
        if "components" not in (getattr(payload, "data", None) or {}):
            return
        channel = self.get_channel(payload.channel_id)
        if not self.watched(channel):
            return
        self.logger(f"Uncached edit in {payload.channel_id}, fetching message...", "DEBUG")
        try:
            message = await channel.fetch_message(payload.message_id)
        except Exception as exc:
            self.logger(f"Could not fetch edited message: {exc}", "WARN")
            return
        await self.scan(message)

    def find_button(self, message: "discord.Message", custom_id: str):
        for button in iter_buttons(getattr(message, "components", None) or []):
            if button.custom_id == custom_id:
                return button
        return None

    def rearm_disabled(self, message: "discord.Message") -> None:
        """Forget clicks on buttons of this message that are disabled again.

        Persistent queue panels edit the same message: the button goes disabled
        (queue closed / already in queue) and later enabled when the queue
        reopens. Without this the message would only ever be clicked once.
        """
        stale = [key for key in self._clicked if key[0] == message.id]
        for key in stale:
            button = self.find_button(message, key[1])
            if button is not None and getattr(button, "disabled", False):
                self._clicked.pop(key, None)
                self.logger(
                    f"Re-armed {button.label or key[1]!r} (button is disabled again)",
                    "DEBUG",
                )

    async def scan(self, message: "discord.Message") -> None:
        author = getattr(message, "author", None)
        if author is not None and self.user is not None and author.id == self.user.id:
            return

        event_at = self._event_at.pop(getattr(message, "id", None), None)
        self.stats["messages"] += 1
        channel = getattr(message, "channel", None)
        channel_name = self.watch.get(getattr(channel, "id", 0)) or getattr(channel, "name", "?")

        buttons = list(iter_buttons(getattr(message, "components", None) or []))
        self.rearm_disabled(message)
        if self.cfg.get("debug_components"):
            if buttons:
                preview = ", ".join(
                    f"{(b.label or '')!r}/{b.custom_id}" + (" (disabled)" if b.disabled else "")
                    for b in buttons
                )
                self.logger(f"[debug] {channel_name}: {preview}", "DEBUG")
            else:
                text = (message.content or "").replace("\n", " ")[:140]
                if text:
                    self.logger(f"[debug] {channel_name} {author}: {text}", "DEBUG")

        for button in buttons:
            if self.matcher.matches(button):
                await self.click(message, button, event_at)

    async def click(self, message: "discord.Message", button: "discord.Button", event_at=None) -> None:
        custom_id = button.custom_id
        if not custom_id:
            return
        key = (message.id, custom_id)
        now = time.monotonic()
        clicked_at = self._clicked.get(key)
        if clicked_at is not None:
            rearm_after = max(0.0, float(self.cfg.get("rearm_after_seconds", 0) or 0))
            if rearm_after <= 0 or now - clicked_at < rearm_after:
                return
            self.logger(f"Re-armed after {rearm_after:g}s: {button.label!r}", "DEBUG")
            self._clicked.pop(key, None)

        cooldown = float(self.cfg.get("click_cooldown_seconds", 0.25))
        if now - self._last_click.get(message.channel.id, 0.0) < cooldown:
            self.logger(f"Cooldown skip: {button.label!r}", "DEBUG")
            return
        self._last_click[message.channel.id] = now
        self._clicked[key] = now
        self.stats["detected"] += 1

        channel_name = self.watch.get(message.channel.id) or getattr(message.channel, "name", "?")
        spoken_channel = re.sub(r"[#/]+", " ", channel_name).strip() or "Discord"
        try:
            age_ms = (
                datetime.now(timezone.utc) - discord.utils.snowflake_time(message.id)
            ).total_seconds() * 1000 + self.clock_offset * 1000
        except Exception:
            age_ms = -1
        if event_at is not None:
            recv_ms = (time.perf_counter() - event_at) * 1000.0
        else:
            recv_ms = -1.0
        detected_line = (
            f"DETECTED  {channel_name}  button={button.label!r}  "
            f"age={age_ms:.0f}ms  recv={recv_ms:.0f}ms"
        )

        if self.cfg.get("dry_run"):
            self.logger(detected_line, "HIT")
            self.logger("DRY RUN - not clicking", "WARN")
            play_alarm(f"Queue open in {spoken_channel}", self.cfg, self.logger)
            return

        attempts = max(1, int(self.cfg.get("retry_attempts", 2)) + 1)
        delay = max(0.0, float(self.cfg.get("retry_delay_ms", 120)) / 1000.0)

        # Start the POST before logging/alerting so the request reaches the
        # network a little sooner. The log is async now, but the alarm spawns a
        # thread, so this keeps that work off the critical path too.
        task = asyncio.ensure_future(
            self._attempt_clicks(message, button, custom_id, attempts, delay)
        )
        await asyncio.sleep(0)  # let the click coroutine start the request

        self.logger(detected_line, "HIT")
        play_alarm(f"Queue open in {spoken_channel}. Go!", self.cfg, self.logger)

        ok, elapsed, last_error = await task
        if ok:
            self.stats["clicks"] += 1
            react_ms = (
                (time.perf_counter() - event_at) * 1000.0
                if event_at is not None
                else elapsed
            )
            self.logger(
                f"CLICKED   ok  http={elapsed:.0f}ms  react={react_ms:.0f}ms  "
                f"total={self.age_ms(message.id, self.clock_offset):.0f}ms",
                "OK",
            )
            return

        self.stats["errors"] += 1
        self._clicked.pop(key, None)  # allow a later edit to retry
        self.logger(f"CLICK FAILED: {last_error}", "ERROR")
        play_error()

    async def _attempt_clicks(
        self,
        message: "discord.Message",
        button: "discord.Button",
        custom_id: str,
        attempts: int,
        delay: float,
    ) -> tuple[bool, float, object]:
        last_error: object = None
        for attempt in range(attempts):
            current = button if attempt == 0 else self.find_button(message, custom_id)
            if current is None or getattr(current, "disabled", False):
                return False, 0.0, "button disabled or gone"

            started = time.perf_counter()
            try:
                await current.click()
                elapsed = (time.perf_counter() - started) * 1000.0
                return True, elapsed, None
            except Exception as exc:
                last_error = exc
                elapsed = (time.perf_counter() - started) * 1000.0
                self.logger(
                    f"Click failed ({attempt + 1}/{attempts}, {elapsed:.0f}ms): {exc}",
                    "WARN",
                )
                if attempt + 1 < attempts:
                    await asyncio.sleep(delay)
        return False, 0.0, last_error

    @staticmethod
    def age_ms(message_id: int, clock_offset: float = 0.0) -> float:
        try:
            return (
                datetime.now(timezone.utc) - discord.utils.snowflake_time(message_id)
            ).total_seconds() * 1000.0 + clock_offset * 1000.0
        except Exception:
            return -1.0


# --------------------------------------------------------------------------
# Self test
# --------------------------------------------------------------------------

def run_self_test(cfg: dict, logger: Logger) -> bool:
    ok = True

    def check(name: str, condition: bool) -> None:
        nonlocal ok
        print(f"  {'PASS' if condition else 'FAIL'}  {name}")
        ok = ok and bool(condition)

    print("\nComponent matching self-test:\n")
    try:
        row = discord.ActionRow(
            {
                "type": 1,
                "components": [
                    {"type": 2, "style": 1, "label": "Enter Waitlist", "custom_id": "join_waitlist"},
                    {"type": 2, "style": 2, "label": "Leave Waitlist", "custom_id": "leave_waitlist"},
                    {"type": 2, "style": 1, "label": "Enter Waitlist", "custom_id": "join2", "disabled": True},
                    {"type": 2, "style": 5, "label": "Enter Waitlist", "url": "https://example.com"},
                ],
            }
        )
    except Exception as exc:
        print(f"  FAIL  could not build components: {exc!r}")
        return False

    buttons = list(iter_buttons([row]))
    matcher = ButtonMatcher(cfg)
    check("found all 4 buttons", len(buttons) == 4)
    check("matches 'Enter Waitlist'", len(buttons) >= 1 and matcher.matches(buttons[0]))
    check("ignores 'Leave Waitlist'", len(buttons) >= 2 and not matcher.matches(buttons[1]))
    check("ignores disabled button", len(buttons) >= 3 and not matcher.matches(buttons[2]))
    check("ignores link button", len(buttons) >= 4 and not matcher.matches(buttons[3]))

    try:
        ids_only = discord.ActionRow(
            {
                "type": 1,
                "components": [
                    {"type": 2, "style": 1, "custom_id": "join_waitlist"},
                    {"type": 2, "style": 1, "custom_id": "leave_waitlist"},
                    {"type": 2, "style": 1, "custom_id": "queue_join"},
                ],
            }
        )
        id_buttons = list(iter_buttons([ids_only]))
        check("matches label-less join_waitlist", len(id_buttons) >= 1 and matcher.matches(id_buttons[0]))
        check("ignores label-less leave_waitlist", len(id_buttons) >= 2 and not matcher.matches(id_buttons[1]))
        check("matches label-less queue_join", len(id_buttons) >= 3 and matcher.matches(id_buttons[2]))
    except Exception as exc:
        print(f"  FAIL  custom_id test: {exc!r}")
        ok = False

    if hasattr(discord, "Container"):
        try:
            container = discord.Container(
                {
                    "type": 17,
                    "components": [
                        {"type": 10, "content": "Queue is open"},
                        {
                            "type": 1,
                            "components": [
                                {"type": 2, "style": 1, "label": "Join Queue", "custom_id": "join_queue"}
                            ],
                        },
                    ],
                }
            )
            v2_buttons = list(iter_buttons([container]))
            check("finds button in Components V2 container", len(v2_buttons) == 1)
            check("matches Components V2 button", bool(v2_buttons) and matcher.matches(v2_buttons[0]))
        except Exception as exc:
            print(f"  WARN  Components V2 test skipped: {exc!r}")
    else:
        print("  WARN  Components V2 not supported by this library version")

    check("parse_indices('1,3-4', 5)", parse_indices("1,3-4", 5) == [0, 2, 3])
    check("deep_merge keeps defaults", deep_merge({"a": 1, "b": {"c": 2}}, {"b": {"d": 3}})["b"] == {"c": 2, "d": 3})
    check("clean_id accepts an ID", clean_id(" 1234567890123456789 ") == "1234567890123456789")
    check("clean_id rejects junk", clean_id("not-an-id") == "")
    check(
        "parse_channel_ref accepts an ID",
        parse_channel_ref(" 1234567890123456789 ") == ("", "1234567890123456789"),
    )
    check(
        "parse_channel_ref accepts a link",
        parse_channel_ref(
            "https://discord.com/channels/111111111111111111/222222222222222222"
        )
        == ("111111111111111111", "222222222222222222"),
    )
    check(
        "resolve_watch uses channel_id",
        resolve_watch({"channel_id": "1234567890123456789"}) == {1234567890123456789: ""},
    )
    check(
        "resolve_server_id uses server_id",
        resolve_server_id({"server_id": "1234567890123456789"}) == 1234567890123456789,
    )
    check(
        "resolve_servers merges server_id and watch_servers",
        resolve_servers(
            {
                "server_id": "1234567890123456789",
                "watch_servers": {"9876543210987654321": "Other"},
            }
        )
        == {1234567890123456789: "", 9876543210987654321: "Other"},
    )
    check(
        "keepalive_seconds default is valid",
        isinstance(DEFAULT_CONFIG.get("keepalive_seconds"), (int, float))
        and DEFAULT_CONFIG["keepalive_seconds"] >= 0,
    )
    check("boost_process default is a bool", isinstance(DEFAULT_CONFIG.get("boost_process"), bool))

    fresh = discord.utils.time_snowflake(datetime.now(timezone.utc))
    fresh_age = QueueSniper.age_ms(fresh)
    check("age_ms is small for a new message", 0 <= fresh_age < 5000)
    check("age_ms handles a junk ID", QueueSniper.age_ms("nope") == -1.0)
    corrected = QueueSniper.age_ms(fresh, -1.0)
    check("age_ms applies the clock offset", abs((corrected - fresh_age) + 1000) < 100)

    # Re-arm: a clicked button that goes disabled must be clickable again later.
    class FakeSniper:
        find_button = QueueSniper.find_button
        rearm_disabled = QueueSniper.rearm_disabled

        def __init__(self, clicked):
            self._clicked = clicked
            self.logger = lambda *args, **kwargs: None

    class FakeMessage:
        def __init__(self, components):
            self.id = 123
            self.components = components

    try:
        fake = FakeSniper({(123, "join_waitlist"): 1.0})
        fake.rearm_disabled(FakeMessage([row]))
        check("keeps click while button is enabled", (123, "join_waitlist") in fake._clicked)

        disabled_row = discord.ActionRow(
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "style": 1,
                        "label": "Enter Waitlist",
                        "custom_id": "join_waitlist",
                        "disabled": True,
                    },
                ],
            }
        )
        fake.rearm_disabled(FakeMessage([disabled_row]))
        check("re-arms after button becomes disabled", (123, "join_waitlist") not in fake._clicked)
    except Exception as exc:
        print(f"  FAIL  re-arm test: {exc!r}")
        ok = False

    print(f"\nResult: {'ALL GOOD' if ok else 'FAILED'}\n")
    return ok


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discord queue sniper")
    parser.add_argument("--setup", action="store_true", help="add/remove watched servers and channels")
    parser.add_argument("--list", action="store_true", help="list servers/channels and IDs")
    parser.add_argument("--dry-run", action="store_true", help="alarm only, do not click")
    parser.add_argument("--debug", action="store_true", help="print buttons seen in watched channels")
    parser.add_argument("--test-alarm", action="store_true", help="test the alarm sound")
    parser.add_argument("--self-test", action="store_true", help="test component matching")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="path to config.json")
    parser.add_argument("--no-pause", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    enable_ansi()
    config_path = Path(args.config).resolve()
    cfg = load_config(config_path)
    tune_process(cfg)
    if args.dry_run:
        cfg["dry_run"] = True
    if args.debug:
        cfg["debug_components"] = True

    logger = Logger(cfg)

    if args.test_alarm:
        logger("Playing test alarm (3 beeps + voice)...", "INFO")
        play_alarm("Test. Queue open.", cfg, logger)
        time.sleep(7)
        return

    if args.self_test:
        raise SystemExit(0 if run_self_test(cfg, logger) else 1)

    # Double-clicking the .exe with no IDs configured should ask for them
    # instead of printing an error and closing.
    if not resolve_watch(cfg) and not (args.setup or args.list):
        if getattr(sys, "frozen", False):
            logger("No server/channel configured yet - starting setup.", "INFO")
            args.setup = True
        else:
            logger("No server/channel configured. Run:  run.bat --setup", "ERROR")
            raise SystemExit(1)

    # Double-clicking the exe should not require remembering a flag: offer a
    # short setup window, then start on its own.
    if not (args.setup or args.list) and ask_setup_or_start(cfg):
        args.setup = True

    token = get_token(interactive=True)
    if not token:
        logger("No token provided.", "ERROR")
        raise SystemExit(1)

    logger("Connecting to Discord...", "INFO")

    if args.list:
        client = ListClient(cfg, logger)
        try:
            client.run(token, log_handler=None)
        except discord.LoginFailure:
            logger("Discord rejected the token. Re-copy it (changing your password invalidates it).", "ERROR")
            raise SystemExit(1)
        except KeyboardInterrupt:
            pass
        except Exception as exc:
            logger(f"List client error: {exc!r}", "ERROR")
            raise SystemExit(1)
        return

    if args.setup:
        prompt_targets(cfg, config_path, logger)
        logger("Starting sniper...", "INFO")

    client = QueueSniper(cfg, logger)
    try:
        client.run(token, log_handler=None)
    except discord.LoginFailure:
        logger("Discord rejected the token. Re-copy it (changing your password invalidates it).", "ERROR")
        raise SystemExit(1)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger(f"Fatal error: {exc!r}", "ERROR")
        raise SystemExit(1)
    finally:
        elapsed = time.time() - client._started
        logger(
            f"Stopped after {elapsed / 60:.1f} min  |  messages={client.stats['messages']} "
            f"detected={client.stats['detected']} clicks={client.stats['clicks']} "
            f"errors={client.stats['errors']}",
            "INFO",
        )


if __name__ == "__main__":
    try:
        main()
    finally:
        if getattr(sys, "frozen", False) and "--no-pause" not in sys.argv:
            try:
                input("\nPress Enter to close...")
            except Exception:
                pass
