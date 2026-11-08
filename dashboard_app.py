#!/usr/bin/env python3
"""
Queue Sniper - App Dashboard (desktop)
--------------------------------------
Native Windows window (WebView2 through pywebview) that shows the dashboard
UI and drives the real sniper from sniper.py.

Run from source:
    python dashboard_app.py

Build a standalone exe:
    build-dashboard.bat

Keep config.json, .env and alarm.wav next to the exe. The sniper resolves
those relative to the executable, so moving the exe alone will not find them.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

try:
    import webview
except ImportError:  # pragma: no cover - shown to the user at runtime
    print("pywebview is missing. Run: pip install -r requirements.txt")
    raise SystemExit(1)

import sniper
from sniper import (
    BASE_DIR,
    DEFAULT_CONFIG_PATH,
    ENV_PATH,
    Logger,
    QueueSniper,
    clean_id,
    get_token,
    load_config,
    normalize_channel_servers,
    parse_channel_ref,
    resolve_servers,
    resolve_watch,
    save_config,
    tune_process,
)

APP_TITLE = "App Dashboard"
APP_VERSION = "1.0.0"

MODE_PRESETS: dict[str, dict[str, Any]] = {
    "turbo": {"retry_attempts": 2, "retry_delay_ms": 120, "click_cooldown_seconds": 0.25},
    "patience": {"retry_attempts": 1, "retry_delay_ms": 260, "click_cooldown_seconds": 0.5},
    "safe": {"retry_attempts": 1, "retry_delay_ms": 450, "click_cooldown_seconds": 1.0},
    "smart": {"retry_attempts": 2, "retry_delay_ms": 180, "click_cooldown_seconds": 0.35},
}

AVATARS = [
    "linear-gradient(135deg, #22c55e, #0ea5e9)",
    "linear-gradient(135deg, #3b82f6, #8b5cf6)",
    "linear-gradient(135deg, #a855f7, #ec4899)",
    "linear-gradient(135deg, #f59e0b, #ef4444)",
    "linear-gradient(135deg, #14b8a6, #22c55e)",
    "linear-gradient(135deg, #6366f1, #22d3ee)",
]


def dashboard_dir() -> Path:
    """Where index.html lives, both from source and inside the onefile exe."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", BASE_DIR)) / "dashboard"
    return Path(__file__).resolve().parent / "dashboard"


def mask_id(value: str) -> str:
    value = str(value or "")
    return ("••••" + value[-4:]) if len(value) > 4 else "••••"


def initials_for(name: str) -> str:
    words = [word for word in str(name or "").split() if word]
    letters = "".join(word[0] for word in words[:2]).upper()
    return letters or "QS"


def avatar_for(key: str) -> str:
    return AVATARS[sum(ord(ch) for ch in str(key)) % len(AVATARS)]


class DashboardLogger(Logger):
    """Logger that also streams lines into the dashboard activity log."""

    def __init__(self, cfg: dict, service: "DashboardService"):
        super().__init__(cfg)
        self.service = service

    def __call__(self, message: str, level: str = "INFO") -> None:
        super().__call__(message, level)
        self.service.push_event(message, level)


class DashboardSniper(QueueSniper):
    """QueueSniper that reports connection state back to the dashboard."""

    def __init__(self, cfg: dict, logger: Logger, service: "DashboardService"):
        super().__init__(cfg, logger)
        self.service = service

    async def on_ready(self) -> None:
        await super().on_ready()
        self.service.mark_online(self)
        self.service.capture_metadata(self)

    async def close(self) -> None:
        self.service.set_status("stopping", "Stopping sniper...")
        await super().close()


class DashboardService:
    """Owns the sniper thread, the event buffer and the config edits."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._events: deque[dict[str, Any]] = deque(maxlen=4000)
        self._seq = 0
        self._cleared_seq = 0
        self._status = "offline"
        self._status_text = "Not logged in"
        self._error = ""
        self._account = ""
        self._connected_at = 0.0
        self._clock_offset = 0.0
        self._token_present = bool(get_token(interactive=False))
        self._client: Optional[DashboardSniper] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._logger: Optional[DashboardLogger] = None
        self._window: Optional[Any] = None
        self._maximized = False
        self._last_click_ms = -1.0
        self._meta: dict[int, dict[str, Any]] = {}
        self._meta_busy = False
        threading.Thread(target=self._measure_clock, name="clock-offset", daemon=True).start()
        self._start_metadata_refresh()

    # -- plumbing ---------------------------------------------------------

    def attach_window(self, window: Any) -> None:
        self._window = window

    def _measure_clock(self) -> None:
        try:
            offset = sniper.measure_clock_offset(0.8)
            with self._lock:
                self._clock_offset = offset
        except Exception:
            pass

    # -- watchlist metadata (server names + icons) ------------------------

    def _discord_get(self, path: str, token: str, timeout: float = 6.0) -> Any:
        request = urllib.request.Request(
            "https://discord.com/api/v10" + path,
            headers={
                "Authorization": token,
                "Accept": "application/json",
                "User-Agent": "QueueSniper/1.0",
            },
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _start_metadata_refresh(self) -> None:
        """Look up server names/icons in the background, without blocking the UI."""
        with self._lock:
            if self._meta_busy:
                return
            self._meta_busy = True
        threading.Thread(
            target=self._resolve_metadata, name="watchlist-meta", daemon=True
        ).start()

    def _config_metadata(self, cfg: dict) -> tuple[dict[int, int], dict[int, str], dict[int, str]]:
        mapping = normalize_channel_servers(cfg.get("channel_servers"))
        names = {int(key): str(value) for key, value in resolve_servers(cfg).items()}
        icons: dict[int, str] = {}
        raw_icons = cfg.get("server_icons")
        if isinstance(raw_icons, dict):
            for key, value in raw_icons.items():
                try:
                    icons[int(key)] = str(value)
                except (TypeError, ValueError):
                    continue
        return mapping, names, icons

    def _resolve_metadata(self) -> None:
        try:
            token = get_token(interactive=False)
            if not token:
                return
            cfg = load_config()
            watch = resolve_watch(cfg)
            if not watch:
                return

            mapping, names, icons = self._config_metadata(cfg)

            # One call gives every guild name + icon hash.
            guilds: dict[int, dict[str, Any]] = {}
            try:
                listing = self._discord_get("/users/@me/guilds", token)
                if isinstance(listing, list):
                    for guild in listing:
                        try:
                            guilds[int(guild["id"])] = guild
                        except (KeyError, TypeError, ValueError):
                            continue
            except Exception:
                pass

            changed = False
            channel_names: dict[int, str] = {}

            # If a channel is not linked to a server (or has no name yet), list
            # the channels of each configured server. This works for channels
            # that return 403 on GET /channels/{id}.
            needs_lookup = any(
                channel_id not in mapping or not str(friendly or "").strip()
                for channel_id, friendly in watch.items()
            )
            if needs_lookup:
                for server_id in list(names):
                    if guilds and server_id not in guilds:
                        continue
                    try:
                        channels = self._discord_get(
                            f"/guilds/{server_id}/channels", token
                        )
                    except Exception:
                        continue
                    if not isinstance(channels, list):
                        continue
                    for channel in channels:
                        try:
                            channel_id = int(channel["id"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        if channel_id not in watch:
                            continue
                        mapping[channel_id] = server_id
                        name = str(channel.get("name") or "")
                        if name:
                            channel_names[channel_id] = name
                        changed = True

            # Fallback for anything the guild lists did not cover.
            for channel_id, friendly in watch.items():
                if channel_id in mapping and str(friendly or "").strip():
                    continue
                try:
                    channel = self._discord_get(f"/channels/{channel_id}", token)
                except Exception:
                    continue
                name = str(channel.get("name") or "")
                if name:
                    channel_names[channel_id] = name
                if channel_id not in mapping and channel.get("guild_id"):
                    try:
                        mapping[channel_id] = int(channel["guild_id"])
                        changed = True
                    except (TypeError, ValueError):
                        pass

            # Refresh server names/icons for every server we know about.
            for server_id in set(list(names) + list(mapping.values())):
                guild = guilds.get(server_id) or {}
                name = str(guild.get("name") or names.get(server_id) or "")
                icon_hash = str(guild.get("icon") or "")
                icon = (
                    f"https://cdn.discordapp.com/icons/{server_id}/"
                    f"{icon_hash}.png?size=64"
                    if icon_hash
                    else ""
                )
                if name and names.get(server_id) != name:
                    names[server_id] = name
                    changed = True
                if guilds and icons.get(server_id, "") != icon:
                    icons[server_id] = icon
                    changed = True

            for channel_id, name in channel_names.items():
                if str(watch.get(channel_id, "")).strip() != name:
                    watch[channel_id] = name
                    changed = True

            updates: dict[int, dict[str, Any]] = {}
            for channel_id, channel_name in watch.items():
                server_id = mapping.get(channel_id, 0)
                info: dict[str, Any] = {}
                if channel_name:
                    info["channel_name"] = str(channel_name)
                if server_id:
                    info.update(
                        {
                            "server_id": str(server_id),
                            "server_name": names.get(server_id, ""),
                            "server_icon": icons.get(server_id, ""),
                        }
                    )
                if info:
                    updates[channel_id] = info

            with self._lock:
                self._meta.update(updates)

            if updates or changed:
                latest = load_config()
                latest["watch_channels"] = {
                    str(channel_id): str(name) for channel_id, name in watch.items()
                }
                latest["channel_servers"] = {
                    str(channel_id): str(server_id)
                    for channel_id, server_id in mapping.items()
                }
                latest["server_icons"] = {
                    str(server_id): icon for server_id, icon in icons.items() if icon
                }
                latest["watch_servers"] = {
                    str(server_id): name for server_id, name in names.items()
                }
                save_config(latest)
        except Exception:
            pass
        finally:
            with self._lock:
                self._meta_busy = False

    def capture_metadata(self, client: DashboardSniper) -> None:
        """Read the real guild/channel names and icons once the sniper is online."""
        try:
            cfg = load_config()
            watch = resolve_watch(cfg)
            mapping, names, icons = self._config_metadata(cfg)
            updates: dict[int, dict[str, Any]] = {}
            changed = False
            for channel_id in watch:
                channel = client.get_channel(channel_id)
                if channel is None:
                    continue
                channel_name = str(getattr(channel, "name", "") or "")
                info: dict[str, Any] = {"channel_name": channel_name}
                if channel_name and str(watch.get(channel_id, "")).strip() != channel_name:
                    watch[channel_id] = channel_name
                    changed = True
                guild = getattr(channel, "guild", None)
                if guild is not None:
                    server_id = int(guild.id)
                    mapping[channel_id] = server_id
                    changed = True
                    guild_name = str(getattr(guild, "name", "") or "")
                    if guild_name:
                        names[server_id] = guild_name
                    icon = ""
                    try:
                        if getattr(guild, "icon", None) is not None:
                            icon = guild.icon.with_size(64).url
                    except Exception:
                        icon = ""
                    icons[server_id] = icon
                    info.update(
                        {
                            "server_id": str(server_id),
                            "server_name": guild_name or names.get(server_id, ""),
                            "server_icon": icon,
                        }
                    )
                updates[channel_id] = info

            with self._lock:
                self._meta.update(updates)

            if changed or updates:
                latest = load_config()
                latest["watch_channels"] = {
                    str(channel_id): str(name) for channel_id, name in watch.items()
                }
                latest["channel_servers"] = {
                    str(channel_id): str(server_id)
                    for channel_id, server_id in mapping.items()
                }
                latest["server_icons"] = {
                    str(server_id): icon for server_id, icon in icons.items() if icon
                }
                latest["watch_servers"] = {
                    str(server_id): name for server_id, name in names.items()
                }
                save_config(latest)
        except Exception:
            pass

    def push_event(self, message: str, level: str = "INFO") -> None:
        with self._lock:
            self._seq += 1
            self._events.append(
                {
                    "seq": self._seq,
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "level": str(level or "INFO").upper(),
                    "message": str(message),
                }
            )

    def set_status(self, status: str, text: str = "", error: str = "") -> None:
        with self._lock:
            self._status = status
            if text:
                self._status_text = text
            self._error = error

    def mark_online(self, client: DashboardSniper) -> None:
        with self._lock:
            self._status = "online"
            self._status_text = "Connected to Discord"
            self._error = ""
            self._account = str(client.user or "")
            self._connected_at = time.time()
            self._token_present = True
        self.push_event("Sniper is online and watching for queues", "OK")

    # -- config -> UI -----------------------------------------------------

    def channels(self) -> list[dict[str, Any]]:
        cfg = load_config()
        watch = resolve_watch(cfg)
        mapping, names, icons = self._config_metadata(cfg)
        with self._lock:
            meta = {key: dict(value) for key, value in self._meta.items()}

        out: list[dict[str, Any]] = []
        for channel_id in watch:
            key = str(channel_id)
            live = meta.get(channel_id, {})

            try:
                server_id = int(live.get("server_id") or mapping.get(channel_id) or 0)
            except (TypeError, ValueError):
                server_id = 0

            server_name = str(live.get("server_name") or names.get(server_id) or "").strip()
            server_icon = str(live.get("server_icon") or icons.get(server_id) or "").strip()

            server_label = server_name or (
                f"Server {mask_id(str(server_id))}" if server_id else "Server not linked"
            )
            out.append(
                {
                    "id": key,
                    "name": server_label[:40],
                    "serverId": str(server_id) if server_id else "",
                    "serverName": server_label[:40],
                    "serverIcon": server_icon,
                    "tag": "WATCH",
                    "tagClass": "tag-green",
                    "initials": initials_for(server_label),
                    "avatar": avatar_for(str(server_id) if server_id else key),
                    "meta": "",
                    "selected": True,
                    "custom": False,
                }
            )
        return out

    def _click_mode(self, cfg: dict) -> str:
        mode = str(cfg.get("click_mode") or "").lower()
        return mode if mode in MODE_PRESETS else "turbo"

    def _watch_count(self, cfg: Optional[dict] = None) -> int:
        return len(resolve_watch(cfg if cfg is not None else load_config()))

    def _stats(self) -> dict[str, int]:
        client = self._client
        if client is None:
            return {"messages": 0, "detected": 0, "clicks": 0, "errors": 0}
        try:
            return {key: int(value) for key, value in client.stats.items()}
        except Exception:
            return {"messages": 0, "detected": 0, "clicks": 0, "errors": 0}

    def _latency(self) -> float:
        client = self._client
        if client is None or self._status != "online":
            return -1.0
        try:
            return round(float(client.latency) * 1000.0, 1)
        except Exception:
            return -1.0

    # -- API: state -------------------------------------------------------

    def get_state(self, since_seq: int = 0) -> dict[str, Any]:
        try:
            since_seq = int(since_seq or 0)
        except (TypeError, ValueError):
            since_seq = 0
        cfg = load_config()
        with self._lock:
            events = [event for event in self._events if event["seq"] > since_seq]
            status = self._status
            status_text = self._status_text
            error = self._error
            seq = self._seq
            cleared = self._cleared_seq
            account = self._account
            offset = self._clock_offset
            token_present = self._token_present
            connected_for = int(time.time() - self._connected_at) if self._connected_at else 0
            click_ms = self._last_click_ms

        if status == "offline" and not token_present:
            status_text = "No Discord token configured"
        elif status == "offline":
            status_text = "Not logged in"
        elif status == "connecting":
            status_text = "Connecting to Discord..."
        elif status == "online":
            status_text = f"Watching {self._watch_count(cfg)} channel" + (
                "" if self._watch_count(cfg) == 1 else "s"
            )
        elif status == "stopping":
            status_text = "Stopping sniper..."

        dashboard_ui = cfg.get("dashboard")
        if not isinstance(dashboard_ui, dict):
            dashboard_ui = {}

        return {
            "desktop": True,
            "version": APP_VERSION,
            "running": status in ("connecting", "online", "stopping"),
            "status": status,
            "status_text": status_text,
            "error": error,
            "account": account,
            "connected_for": connected_for,
            "seq": seq,
            "cleared_at": cleared,
            "events": events,
            "channels": self.channels(),
            "watch_count": self._watch_count(cfg),
            "token_present": token_present,
            "click_mode": self._click_mode(cfg),
            "clock_offset": round(offset, 2),
            "latency_ms": self._latency(),
            "last_click_ms": click_ms,
            "stats": self._stats(),
            "ui": dashboard_ui,
            "config_path": str(DEFAULT_CONFIG_PATH),
            "env_path": str(ENV_PATH),
        }

    def clear_log(self) -> dict[str, Any]:
        with self._lock:
            self._events.clear()
            self._cleared_seq = self._seq
            seq = self._seq
        return {"ok": True, "seq": seq}

    # -- API: sniper lifecycle -------------------------------------------

    def start_sniper(self) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return {"ok": False, "error": "Sniper is already running."}
            if self._status == "connecting":
                return {"ok": False, "error": "Already connecting."}

        cfg = load_config()
        watch = resolve_watch(cfg)
        if not watch:
            self.push_event("No channels selected. Pick at least one and save.", "ERROR")
            self.set_status("error", "No channels configured", "No channels selected")
            return {"ok": False, "error": "No channels selected. Add a channel and save first."}

        token = get_token(interactive=False)
        with self._lock:
            self._token_present = bool(token)
        if not token:
            message = f"No Discord token. Add DISCORD_TOKEN to {ENV_PATH.name} next to the app."
            self.push_event(message, "ERROR")
            self.set_status("error", "No Discord token", message)
            return {"ok": False, "error": message}

        tune_process(cfg)
        self._logger = DashboardLogger(cfg, self)
        self.push_event("Starting sniper...", "INFO")
        self.set_status("connecting", "Connecting to Discord...")

        thread = threading.Thread(
            target=self._run_sniper,
            args=(token, cfg),
            name="queue-sniper",
            daemon=True,
        )
        with self._lock:
            self._thread = thread
        thread.start()
        return {"ok": True}

    def _run_sniper(self, token: str, cfg: dict) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        logger = self._logger or DashboardLogger(cfg, self)
        client = DashboardSniper(cfg, logger, self)
        with self._lock:
            self._loop = loop
            self._client = client

        failure: Optional[str] = None
        try:
            loop.run_until_complete(client.start(token))
        except asyncio.CancelledError:
            pass
        except sniper.discord.LoginFailure:
            failure = "Discord rejected the token. Copy a fresh token into .env."
        except Exception as exc:  # network errors, missing permissions, etc.
            failure = f"Sniper error: {exc!r}"
        finally:
            try:
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
            with self._lock:
                self._client = None
                self._loop = None
                self._thread = None
                self._connected_at = 0.0
                was_stopping = self._status == "stopping"
            if failure:
                self.push_event(failure, "ERROR")
                self.set_status("error", "Connection failed", failure)
            else:
                self.push_event("Sniper offline", "WARN" if was_stopping else "INFO")
                self.set_status("offline", "Not logged in")

    def stop_sniper(self, wait: bool = True) -> dict[str, Any]:
        with self._lock:
            client = self._client
            loop = self._loop
            thread = self._thread
        if client is None or loop is None or not loop.is_running():
            self.set_status("offline", "Not logged in")
            return {"ok": True, "stopped": True}
        self.set_status("stopping", "Stopping sniper...")
        try:
            future = asyncio.run_coroutine_threadsafe(client.close(), loop)
            if wait:
                future.result(timeout=5)
        except Exception as exc:
            self.push_event(f"Sniper stop warning: {exc!r}", "WARN")
            if wait:
                try:
                    client._connection.close()  # best effort
                except Exception:
                    pass
        if wait and thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
        return {"ok": True, "stopped": True}

    # -- API: settings ----------------------------------------------------

    def save_settings(self, payload: Optional[dict] = None) -> dict[str, Any]:
        payload = payload or {}
        ids = payload.get("channels") or []
        try:
            wanted = [str(int(str(item))) for item in ids]
        except (TypeError, ValueError):
            return {"ok": False, "error": "Channel list was not valid."}
        if not wanted:
            return {"ok": False, "error": "Select at least one channel."}

        cfg = load_config()
        watch = resolve_watch(cfg)
        keep = {int(channel_id): watch.get(int(channel_id), "") for channel_id in wanted}
        cfg["watch_channels"] = {str(key): value for key, value in keep.items()}
        mapping = normalize_channel_servers(cfg.get("channel_servers"))
        cfg["channel_servers"] = {
            str(channel_id): str(server_id)
            for channel_id, server_id in mapping.items()
            if channel_id in keep
        }
        cfg["channel_id"] = ""
        cfg["server_id"] = ""

        mode = str(payload.get("clickMode") or self._click_mode(cfg)).lower()
        if mode not in MODE_PRESETS:
            mode = "turbo"
        cfg["click_mode"] = mode
        cfg.update(MODE_PRESETS[mode])
        save_config(cfg)

        self._apply_live_settings(cfg, keep, mode)
        removed = len(watch) - len(keep)
        message = f"Saved {len(keep)} channel" + ("" if len(keep) == 1 else "s")
        if removed > 0:
            message += f" · removed {removed}"
        self.push_event(message + " · " + mode.title() + " mode", "OK")
        return {"ok": True, "message": message}

    def _apply_live_settings(self, cfg: dict, watch: dict[int, str], mode: str) -> None:
        with self._lock:
            client = self._client
        if client is None:
            return
        try:
            client.watch = dict(watch)
            client.servers = resolve_servers(cfg)
            client.cfg.update(MODE_PRESETS[mode])
            client.cfg["click_mode"] = mode
        except Exception:
            pass

    def set_click_mode(self, mode: str) -> dict[str, Any]:
        mode = str(mode or "").lower()
        if mode not in MODE_PRESETS:
            return {"ok": False, "error": "Unknown click mode."}
        cfg = load_config()
        cfg["click_mode"] = mode
        cfg.update(MODE_PRESETS[mode])
        save_config(cfg)
        self._apply_live_settings(cfg, resolve_watch(cfg), mode)
        self.push_event(f"Click mode set to {mode.title()}", "INFO")
        return {"ok": True, "mode": mode}

    def save_ui_settings(self, payload: Optional[dict] = None) -> dict[str, Any]:
        payload = payload or {}
        cfg = load_config()
        dashboard = cfg.get("dashboard")
        if not isinstance(dashboard, dict):
            dashboard = {}
        for key in ("volume", "desktop_notifications", "sound_notifications", "notify"):
            if key in payload:
                value = payload[key]
                if key == "volume":
                    try:
                        value = max(0, min(100, int(value)))
                    except (TypeError, ValueError):
                        continue
                if key == "notify" and not isinstance(value, dict):
                    continue
                dashboard[key] = value
        dashboard["option"] = "B" if payload.get("option") == "B" else "A"
        cfg["dashboard"] = dashboard
        save_config(cfg)
        return {"ok": True}

    def add_channel(self, name: str = "", server_id: str = "", channel_id: str = "") -> dict[str, Any]:
        name = str(name or "").strip()[:60]
        link_server, channel = parse_channel_ref(str(channel_id or ""))
        given_server = clean_id(server_id)

        if not channel:
            return {
                "ok": False,
                "error": "Paste a channel link or a 15-22 digit channel ID.",
            }
        if link_server and given_server and link_server != given_server:
            return {
                "ok": False,
                "error": "The server ID and the channel link point to different servers.",
            }
        server = given_server or link_server
        if not server:
            return {
                "ok": False,
                "error": "Enter the server ID as well as the channel ID.",
            }

        cfg = load_config()
        watch = resolve_watch(cfg)
        watch[int(channel)] = ""
        cfg["watch_channels"] = {str(key): value for key, value in watch.items()}
        cfg["channel_id"] = ""
        cfg["server_id"] = ""

        servers = resolve_servers(cfg)
        server_label = name or servers.get(int(server)) or f"Server {mask_id(server)}"
        servers[int(server)] = server_label
        cfg["watch_servers"] = {str(key): value for key, value in servers.items()}

        mapping = normalize_channel_servers(cfg.get("channel_servers"))
        mapping[int(channel)] = int(server)
        cfg["channel_servers"] = {
            str(key): str(value) for key, value in mapping.items()
        }

        save_config(cfg)
        self._apply_live_settings(cfg, watch, self._click_mode(cfg))
        self.push_event(
            f"Channel added: {server_label} / {mask_id(channel)}", "OK"
        )
        self._start_metadata_refresh()
        return {
            "ok": True,
            "name": server_label,
            "server_id": server,
            "channel_id": channel,
        }

    def remove_channel(self, channel_id: str = "") -> dict[str, Any]:
        target = clean_id(channel_id)
        if not target:
            return {"ok": False, "error": "Channel was not valid."}
        cfg = load_config()
        watch = resolve_watch(cfg)
        removed = watch.pop(int(target), None)
        if removed is None:
            return {"ok": False, "error": "That channel is not in the watchlist."}
        if not watch:
            watch[int(target)] = removed
            return {"ok": False, "error": "Keep at least one channel in the watchlist."}
        cfg["watch_channels"] = {str(key): value for key, value in watch.items()}
        mapping = normalize_channel_servers(cfg.get("channel_servers"))
        mapping.pop(int(target), None)
        cfg["channel_servers"] = {
            str(key): str(value) for key, value in mapping.items()
        }
        cfg["channel_id"] = ""
        cfg["server_id"] = ""
        save_config(cfg)
        self._apply_live_settings(cfg, watch, self._click_mode(cfg))
        self.push_event(f"Channel removed: {mask_id(target)}", "WARN")
        return {"ok": True}

    # -- API: diagnostics / sounds / webhook ------------------------------

    def run_diagnostics(self) -> dict[str, Any]:
        cfg = load_config()
        watch = resolve_watch(cfg)
        token = get_token(interactive=False)
        with self._lock:
            offset = self._clock_offset
            client = self._client
            status = self._status
        lines: list[list[str]] = [["$ queue-sniper doctor", "dim"]]
        lines.append(
            [
                "[1/5] reading .env .................. " + ("token found" if token else "NO TOKEN"),
                "ok" if token else "err",
            ]
        )
        if abs(offset) < 0.001:
            offset = sniper.measure_clock_offset(0.8)
            with self._lock:
                self._clock_offset = offset
        lines.append(
            [
                f"[2/5] clock offset .................. {offset:+.2f} s (applied)",
                "ok" if abs(offset) <= 5 else "warn",
            ]
        )
        if client is not None and status == "online":
            latency = self._latency()
            lines.append([f"[3/5] gateway ....................... {latency:.0f} ms", "ok"])
            visible = 0
            for channel_id in watch:
                try:
                    if client.get_channel(channel_id) is not None:
                        visible += 1
                except Exception:
                    pass
            kind = "ok" if visible == len(watch) and watch else "warn"
            lines.append(
                [
                    f"[4/5] channel access ................ {visible} / {len(watch)} visible",
                    kind,
                ]
            )
            lines.append(
                [
                    "[5/5] click path .................... connection warm, waiting for a queue",
                    "info",
                ]
            )
            lines.append(["ALL GOOD - sniper is watching", "ok"])
        else:
            lines.append(["[3/5] gateway ....................... not connected (press START)", "warn"])
            lines.append(
                [
                    f"[4/5] channel access ................ {len(watch)} configured, start to verify",
                    "info",
                ]
            )
            lines.append(["[5/5] click path .................... idle", "dim"])
            if token and watch:
                lines.append(["READY - press START to connect", "ok"])
            else:
                lines.append(["NOT READY - fix the items above", "err"])

        lines.append(["", "dim"])
        lines.append(["--- nerd stats ---", "dim"])
        lines.append([f"config ....................... {DEFAULT_CONFIG_PATH}", "dim"])
        lines.append([f"keepalive .................... GET /users/@me", "dim"])
        lines.append([f"clock offset ................. {offset:+.2f} s", "dim"])
        lines.append([f"timer resolution ............. 1 ms", "dim"])
        lines.append([f"click mode ................... {self._click_mode(cfg)}", "dim"])
        lines.append([f"dry run ...................... {bool(cfg.get('dry_run'))}", "dim"])

        self.push_event("Diagnostics complete", "OK")
        return {"ok": True, "lines": lines}

    def test_alarm(self) -> dict[str, Any]:
        cfg = load_config()
        logger = self._logger or DashboardLogger(cfg, self)
        sniper.play_alarm("Test. Queue open.", cfg, logger)
        return {"ok": True}

    def test_webhook(self, url: str = "") -> dict[str, Any]:
        url = str(url or "").strip()
        if not url:
            return {"ok": False, "error": "Paste a webhook URL first."}
        if not url.lower().startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")):
            return {"ok": False, "error": "That does not look like a Discord webhook URL."}
        body = b'{"content":"Queue Sniper test notification","username":"Queue Sniper"}'
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "QueueSniper/1.0"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                code = int(response.status)
        except urllib.error.HTTPError as exc:
            self.push_event(f"Webhook test failed: HTTP {exc.code}", "ERROR")
            return {"ok": False, "error": f"Webhook rejected the test (HTTP {exc.code})."}
        except Exception as exc:
            self.push_event(f"Webhook test failed: {exc}", "ERROR")
            return {"ok": False, "error": f"Webhook test failed: {exc}"}
        self.push_event(f"Webhook delivered · HTTP {code}", "OK")
        return {"ok": True, "status": code}

    # -- API: window ------------------------------------------------------

    def minimize_window(self) -> dict[str, Any]:
        if self._window is not None:
            self._window.minimize()
        return {"ok": True}

    def toggle_maximize(self) -> dict[str, Any]:
        if self._window is not None:
            if self._maximized:
                self._window.restore()
            else:
                self._window.maximize()
            self._maximized = not self._maximized
        return {"ok": True, "maximized": self._maximized}

    def close_app(self) -> dict[str, Any]:
        self.stop_sniper(wait=True)
        if self._window is not None:
            self._window.destroy()
        return {"ok": True}

    def log_client(self, message: str, level: str = "INFO") -> dict[str, Any]:
        """Let the UI write a line into the shared activity log."""
        level = str(level or "INFO").upper()
        if level not in ("DEBUG", "INFO", "OK", "HIT", "WARN", "ERROR"):
            level = "INFO"
        self.push_event(message, level)
        return {"ok": True}


# --------------------------------------------------------------------------
# Local static server (only bound to 127.0.0.1)
# --------------------------------------------------------------------------

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


def serve_dashboard(directory: Path) -> ThreadingHTTPServer:
    handler = functools.partial(QuietHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, name="dashboard-http", daemon=True)
    thread.start()
    return server


def main() -> None:
    if not (dashboard_dir() / "index.html").is_file():
        print(f"Dashboard files are missing from {dashboard_dir()}")
        raise SystemExit(1)

    sniper.enable_ansi()
    service = DashboardService()
    server = serve_dashboard(dashboard_dir())
    url = f"http://127.0.0.1:{server.server_address[1]}/index.html"

    storage = Path(os.environ.get("LOCALAPPDATA", str(BASE_DIR))) / "QueueSniper" / "webview"
    try:
        storage.mkdir(parents=True, exist_ok=True)
    except Exception:
        storage = BASE_DIR / ".webview"

    window = webview.create_window(
        APP_TITLE,
        url,
        js_api=service,
        width=1480,
        height=920,
        min_size=(1024, 700),
        resizable=True,
        frameless=True,
        easy_drag=False,
        background_color="#0b0b0c",
        text_select=False,
        confirm_close=False,
    )
    service.attach_window(window)

    def on_closing() -> None:
        service.stop_sniper(wait=True)

    try:
        window.events.closing += on_closing
    except Exception:
        pass

    try:
        webview.start(
            private_mode=False,
            storage_path=str(storage),
            debug=False,
            menu=[],
        )
    finally:
        service.stop_sniper(wait=True)
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
