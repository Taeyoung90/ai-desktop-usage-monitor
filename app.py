from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_TITLE = "AI Usage Monitor"
REFRESH_MS = 30_000
TOPMOST_ENFORCE_MS = 1_500
STALE_HOURS = 24
WINDOWS_APPS_DIR = Path("C:/Program Files/WindowsApps")
APP_DIR = Path(__file__).resolve().parent
APP_ICON_PNG = APP_DIR / "assets" / "ai-usage-monitor.png"
APP_ICON_ICO = APP_DIR / "assets" / "ai-usage-monitor.ico"
APP_TITLEBAR_ICON_PNG = APP_DIR / "assets" / "ai-usage-monitor-titlebar.png"
REFRESH_ICON_PNG = APP_DIR / "assets" / "refresh.png"


@dataclass
class UsageMetric:
    label: str
    used_percent: float | None = None
    reset_at: datetime | None = None
    detail: str = ""


@dataclass
class ProviderUsage:
    provider: str
    display_name: str
    source: str
    confidence: str
    observed_at: datetime | None = None
    status: str = "ok"
    message: str = ""
    metrics: list[UsageMetric] = field(default_factory=list)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "display_name": self.display_name,
            "source": self.source,
            "confidence": self.confidence,
            "observed_at": iso_or_none(self.observed_at),
            "status": self.status,
            "message": self.message,
            "metrics": [
                {
                    "label": metric.label,
                    "used_percent": metric.used_percent,
                    "reset_at": iso_or_none(metric.reset_at),
                    "detail": metric.detail,
                }
                for metric in self.metrics
            ],
        }


def iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone().isoformat(timespec="seconds")


def dt_from_epoch(value: Any, *, milliseconds: bool = False) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
        if milliseconds or number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc).astimezone()
    except Exception:
        return None


def safe_percent(value: Any) -> float | None:
    try:
        number = float(value)
    except Exception:
        return None
    if number <= 1:
        number *= 100
    return max(0.0, min(100.0, number))


def home_path(*parts: str) -> Path:
    return Path.home().joinpath(*parts)


def display_path(path: Path) -> str:
    try:
        raw = str(path)
        home = str(Path.home())
        if raw.lower().startswith(home.lower()):
            return "~" + raw[len(home) :]
        return raw
    except Exception:
        return str(path)


def collect_codex_usage() -> ProviderUsage:
    sessions_dir = home_path(".codex", "sessions")
    result = ProviderUsage(
        provider="codex",
        display_name="ChatGPT / Codex",
        source=display_path(sessions_dir),
        confidence="high",
    )

    if not sessions_dir.exists():
        result.status = "missing"
        result.message = "Codex sessions folder was not found."
        return result

    newest: tuple[datetime, dict[str, Any], Path] | None = None
    try:
        files = sorted(
            sessions_dir.rglob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except Exception as exc:
        result.status = "error"
        result.message = f"Could not scan Codex sessions: {exc}"
        return result

    for path in files[:300]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for line in reversed(lines):
            if "rate_limits" not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            rate_limits = event.get("rate_limits") or event.get("payload", {}).get("rate_limits")
            if not isinstance(rate_limits, dict):
                continue
            observed = parse_event_time(event.get("timestamp")) or datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).astimezone()
            if newest is None or observed > newest[0]:
                newest = (observed, rate_limits, path)
        if newest:
            break

    if not newest:
        result.status = "missing"
        result.message = "No Codex rate limit event was found yet."
        return result

    observed, rate_limits, path = newest
    result.observed_at = observed
    result.source = display_path(path)
    plan_type = rate_limits.get("plan_type")
    if plan_type:
        result.message = f"Plan: {plan_type}"

    for key, fallback_label in (("primary", "Main window"), ("secondary", "Secondary window"), ("credits", "Credits")):
        window = rate_limits.get(key)
        if not isinstance(window, dict):
            continue
        pct = safe_percent(window.get("used_percent"))
        if pct is None:
            continue
        window_minutes = window.get("window_minutes")
        label = fallback_label
        if window_minutes == 10080:
            label = "Weekly"
        elif window_minutes == 300:
            label = "5 hours"
        elif isinstance(window_minutes, (int, float)) and window_minutes:
            label = f"{int(window_minutes)} min"
        result.metrics.append(
            UsageMetric(
                label=label,
                used_percent=pct,
                reset_at=dt_from_epoch(window.get("resets_at")),
                detail=key,
            )
        )

    if not result.metrics:
        result.status = "error"
        result.message = "Codex rate limit event did not contain readable percentages."
    mark_stale_if_needed(result)
    return result


def parse_event_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def collect_claude_usage() -> ProviderUsage:
    base = home_path(
        "AppData",
        "Local",
        "Packages",
        "Claude_pzs8sxrjxfjjc",
        "LocalCache",
        "Roaming",
        "Claude",
    )
    history_path = base / "plan-usage-history.json"
    ui_usage = collect_claude_ui_usage()
    log_usage = collect_claude_log_usage(base)
    result = ProviderUsage(
        provider="claude",
        display_name="Claude Desktop",
        source=display_path(history_path),
        confidence="medium",
    )

    if not history_path.exists():
        if ui_usage is not None:
            return ui_usage
        if log_usage is not None:
            mark_stale_if_needed(log_usage)
            return log_usage
        result.status = "missing"
        result.message = "Claude plan usage cache was not found."
        return result

    try:
        data = json.loads(history_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        result.status = "error"
        result.message = f"Could not read Claude usage cache: {exc}"
        return result

    samples = data.get("samples")
    if not isinstance(samples, list) or not samples:
        result.status = "missing"
        result.message = "Claude usage cache has no samples."
        return result

    latest = samples[-1]
    usage = latest.get("u") if isinstance(latest, dict) else None
    if not isinstance(usage, dict):
        result.status = "error"
        result.message = "Claude latest sample has no usage payload."
        return result

    result.observed_at = dt_from_epoch(latest.get("t"), milliseconds=True)
    result.message = f"{len(samples)} cached samples"

    label_map = {
        "fh": "5 hours",
        "sd": "Sonnet",
        "wk": "Weekly",
        "7d": "Weekly",
    }
    for key, value in usage.items():
        pct = safe_percent(value)
        if pct is None:
            continue
        result.metrics.append(
            UsageMetric(
                label=label_map.get(str(key), str(key).upper()),
                used_percent=pct,
                detail=f"internal:{key}",
            )
        )

    if not result.metrics:
        result.status = "error"
        result.message = "Claude usage cache did not contain readable percentages."
    mark_stale_if_needed(result)

    if ui_usage is not None:
        return ui_usage
    if log_usage is not None:
        if result.observed_at is None or (
            log_usage.observed_at is not None and log_usage.observed_at > result.observed_at
        ):
            mark_stale_if_needed(log_usage)
            return log_usage
        if any(metric.reset_at for metric in log_usage.metrics) and not any(metric.reset_at for metric in result.metrics):
            result.message = f"{result.message}; reset data available from older logs"
    return result


def collect_claude_ui_usage() -> ProviderUsage | None:
    text = read_windows_accessibility_text("claude")
    if not text:
        return None
    metrics = parse_usage_metrics_from_text(text)
    if not metrics:
        return None
    return ProviderUsage(
        provider="claude",
        display_name="Claude Desktop",
        source="Windows UI Automation",
        confidence="low",
        observed_at=datetime.now().astimezone(),
        message="experimental UI text fallback",
        metrics=metrics,
    )


def read_windows_accessibility_text(process_name: str) -> str:
    script = rf"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$desktop = [System.Windows.Automation.AutomationElement]::RootElement
$processes = Get-Process | Where-Object {{ $_.ProcessName -eq '{process_name}' }}
$names = New-Object System.Collections.Generic.List[string]
foreach ($proc in $processes) {{
  $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ProcessIdProperty, $proc.Id)
  $items = $desktop.FindAll([System.Windows.Automation.TreeScope]::Subtree, $cond)
  foreach ($item in $items) {{
    $name = $item.Current.Name
    if ($name -and $name.Length -lt 240) {{ $names.Add($name) }}
  }}
}}
$names | Select-Object -Unique -First 300
"""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=4,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout


def parse_usage_metrics_from_text(text: str) -> list[UsageMetric]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    metrics: list[UsageMetric] = []
    for index, line in enumerate(lines):
        lowered = line.lower()
        if not any(token in lowered for token in ("usage", "limit", "remaining", "reset", "사용", "남음", "제한", "sonnet")):
            continue
        window = " ".join(lines[index : index + 4])
        percent_match = re.search(r"(\d+(?:\.\d+)?)\s*%", window)
        if not percent_match:
            continue
        label = "Usage"
        if "sonnet" in lowered:
            label = "Sonnet"
        elif "5" in lowered or "five" in lowered:
            label = "5 hours"
        elif "week" in lowered or "7d" in lowered:
            label = "Weekly"
        metrics.append(
            UsageMetric(
                label=label,
                used_percent=safe_percent(percent_match.group(1)),
                detail="ui",
            )
        )
    return dedupe_metrics(metrics)


def dedupe_metrics(metrics: list[UsageMetric]) -> list[UsageMetric]:
    seen: set[tuple[str, float | None]] = set()
    deduped: list[UsageMetric] = []
    for metric in metrics:
        key = (metric.label, metric.used_percent)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(metric)
    return deduped


def collect_claude_log_usage(base: Path) -> ProviderUsage | None:
    logs_dir = base / "logs"
    if not logs_dir.exists():
        return None

    newest: tuple[datetime, dict[str, Any], Path] | None = None
    try:
        log_files = sorted(logs_dir.glob("*.log"), key=lambda path: path.stat().st_mtime, reverse=True)
    except Exception:
        return None

    for path in log_files[:4]:
        try:
            lines = read_recent_text(path, max_bytes=1_000_000).splitlines()
        except Exception:
            continue
        for line in reversed(lines):
            if '"windows"' not in line and '"rate_limit_info"' not in line:
                continue
            observed = parse_claude_log_timestamp(line) or datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).astimezone()
            payload = extract_claude_limit_payload(line)
            if not payload:
                continue
            if newest is None or observed > newest[0]:
                newest = (observed, payload, path)
        if newest:
            break

    if newest is None:
        return None

    observed, payload, path = newest
    result = ProviderUsage(
        provider="claude",
        display_name="Claude Desktop",
        source=display_path(path),
        confidence="medium",
        observed_at=observed,
        message="rate-limit signal from Claude logs",
    )

    windows = payload.get("windows")
    if isinstance(windows, dict):
        label_map = {
            "5h": "5 hours",
            "7d": "Weekly",
            "7d_sonnet": "Sonnet weekly",
        }
        for key in ("5h", "7d", "7d_sonnet"):
            window = windows.get(key)
            if not isinstance(window, dict):
                continue
            result.metrics.append(
                UsageMetric(
                    label=label_map.get(key, key),
                    used_percent=safe_percent(window.get("utilization")),
                    reset_at=dt_from_epoch(window.get("resets_at")),
                    detail=str(window.get("status") or f"log:{key}"),
                )
            )
        status = payload.get("type") or payload.get("representativeClaim")
        if status:
            result.message = f"{result.message}; {status}"
        if result.metrics:
            return result

    rate_limit_info = payload.get("rate_limit_info")
    if isinstance(rate_limit_info, dict):
        result.metrics.append(
            UsageMetric(
                label=str(rate_limit_info.get("rateLimitType") or "Rate limit"),
                reset_at=dt_from_epoch(rate_limit_info.get("resetsAt")),
                detail=str(rate_limit_info.get("status") or "log"),
            )
        )
        return result

    return None


def read_recent_text(path: Path, max_bytes: int) -> str:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
        return handle.read().decode("utf-8", errors="replace")


def parse_claude_log_timestamp(line: str) -> datetime | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})", line)
    if not match:
        return None
    try:
        local_time = datetime.strptime(" ".join(match.groups()), "%Y-%m-%d %H:%M:%S")
        return local_time.astimezone()
    except Exception:
        return None


def extract_claude_limit_payload(line: str) -> dict[str, Any] | None:
    for candidate in iter_json_objects_from_line(line):
        for payload in walk_limit_payloads(candidate):
            return payload
    return None


def iter_json_objects_from_line(line: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    start = line.find("{")
    if start == -1:
        return objects
    decoder = json.JSONDecoder()
    try:
        parsed, _ = decoder.raw_decode(line[start:])
    except json.JSONDecodeError:
        return objects
    if isinstance(parsed, dict):
        objects.append(parsed)
    return objects


def walk_limit_payloads(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if isinstance(value.get("windows"), dict) or isinstance(value.get("rate_limit_info"), dict):
            found.append(value)
        for key in ("message", "rawBody", "error_message"):
            nested = value.get(key)
            if isinstance(nested, str) and "{" in nested:
                for nested_obj in iter_json_objects_from_line(nested):
                    found.extend(walk_limit_payloads(nested_obj))
        for key in ("error", "extra", "details"):
            nested = value.get(key)
            if isinstance(nested, (dict, list)):
                found.extend(walk_limit_payloads(nested))
    elif isinstance(value, list):
        for item in value:
            found.extend(walk_limit_payloads(item))
    return found


def mark_stale_if_needed(result: ProviderUsage) -> None:
    if result.status != "ok" or result.observed_at is None:
        return
    age = datetime.now().astimezone() - result.observed_at.astimezone()
    if age.total_seconds() > STALE_HOURS * 60 * 60:
        hours = int(age.total_seconds() // 3600)
        result.status = "stale"
        prefix = f"stale: last sample {hours}h ago"
        result.message = f"{prefix}; {result.message}" if result.message else prefix


def collect_all() -> list[ProviderUsage]:
    return [collect_codex_usage(), collect_claude_usage()]


def find_installed_app_icon(provider: str) -> Path | None:
    bundled_icons = {
        "codex": APP_DIR / "assets" / "chatgpt.png",
        "claude": APP_DIR / "assets" / "claude.png",
    }
    bundled_icon = bundled_icons.get(provider)
    if bundled_icon is not None and bundled_icon.exists():
        return bundled_icon

    if not WINDOWS_APPS_DIR.exists():
        return None

    if provider == "codex":
        package_patterns = ("OpenAI.Codex_*",)
        relative_candidates = (
            Path("assets/icon.png"),
            Path("app/resources/default_app/icon.png"),
            Path("assets/Square44x44Logo.scale-200.png"),
        )
    elif provider == "claude":
        package_patterns = ("Claude_*",)
        relative_candidates = (
            Path("assets/icon.png"),
            Path("app/resources/ion-dist/images/claude_app_icon.png"),
            Path("assets/Square44x44Logo.scale-200.png"),
        )
    else:
        return None

    packages: list[Path] = []
    for pattern in package_patterns:
        try:
            packages.extend(WINDOWS_APPS_DIR.glob(pattern))
        except Exception:
            continue

    for package in sorted(packages, key=lambda path: path.stat().st_mtime, reverse=True):
        for relative in relative_candidates:
            candidate = package / relative
            if candidate.exists():
                return candidate
    return None


def percent_text(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:.0f}%"


def reset_text(value: datetime | None) -> str:
    if value is None:
        return "reset unknown"
    now = datetime.now().astimezone()
    delta = value - now
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes <= 0:
        return "reset due"
    days, minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"resets in {days}d {hours}h"
    if hours:
        return f"resets in {hours}h {minutes}m"
    return f"resets in {minutes}m"


def status_color(status: str) -> str:
    return {
        "ok": "#62d6c4",
        "stale": "#e5b567",
        "missing": "#e5b567",
        "error": "#ef6f6c",
    }.get(status, "#8f97a6")


def usage_alert_color(value: float | None, default: str) -> str:
    if value is None:
        return default
    if value >= 90:
        return "#ff5f6d"
    if value >= 50:
        return "#f6c85f"
    return default


class WindowsTrayIcon:
    """Windows notification-area icon hosted by a dedicated hidden window."""

    WM_TRAY = 0x0400 + 42
    WM_TRAY_EXIT = 0x0400 + 43
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_CONTEXTMENU = 0x007B
    WM_NULL = 0x0000
    WM_COMMAND = 0x0111
    NIM_ADD = 0x00000000
    NIM_DELETE = 0x00000002
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004
    MF_STRING = 0x00000000
    MF_SEPARATOR = 0x00000800
    TPM_RIGHTBUTTON = 0x00000002
    TPM_RETURNCMD = 0x00000100
    MENU_FULL = 1001
    MENU_COMPACT = 1002
    MENU_EXIT = 1003
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x00000010

    def __init__(
        self,
        root: Any,
        icon_path: Path,
        tooltip: str = APP_TITLE,
        on_show_full: Any | None = None,
        on_show_compact: Any | None = None,
        on_exit: Any | None = None,
    ) -> None:
        self.root = root
        self.icon_path = icon_path
        self.tooltip = tooltip[:127]
        self.on_show_full = on_show_full
        self.on_show_compact = on_show_compact
        self.on_exit = on_exit
        self.enabled = sys.platform == "win32"
        self.added = False
        self._commands: queue.Queue[str] = queue.Queue()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self.hwnd: int | None = None
        self.hicon: int | None = None
        self._wndproc_ref: Any = None
        self._nid: Any = None

    def install(self) -> bool:
        if not self.enabled:
            return False
        self._thread = threading.Thread(target=self._run_message_window, name="AIUsageMonitorTray", daemon=True)
        self._thread.start()
        return self._ready.wait(timeout=2.0) and self.added

    def _run_message_window(self) -> None:
        try:
            from ctypes import wintypes

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [
                    ("style", wintypes.UINT),
                    ("lpfnWndProc", ctypes.c_void_p),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                ]

            class POINT(ctypes.Structure):
                _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

            class MSG(ctypes.Structure):
                _fields_ = [
                    ("hwnd", wintypes.HWND),
                    ("message", wintypes.UINT),
                    ("wParam", wintypes.WPARAM),
                    ("lParam", wintypes.LPARAM),
                    ("time", wintypes.DWORD),
                    ("pt", POINT),
                ]

            class GUID(ctypes.Structure):
                _fields_ = [
                    ("Data1", wintypes.DWORD),
                    ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8),
                ]

            class NOTIFYICONDATAW(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("hWnd", wintypes.HWND),
                    ("uID", wintypes.UINT),
                    ("uFlags", wintypes.UINT),
                    ("uCallbackMessage", wintypes.UINT),
                    ("hIcon", wintypes.HICON),
                    ("szTip", wintypes.WCHAR * 128),
                    ("dwState", wintypes.DWORD),
                    ("dwStateMask", wintypes.DWORD),
                    ("szInfo", wintypes.WCHAR * 256),
                    ("uTimeoutOrVersion", wintypes.UINT),
                    ("szInfoTitle", wintypes.WCHAR * 64),
                    ("dwInfoFlags", wintypes.DWORD),
                    ("guidItem", GUID),
                    ("hBalloonIcon", wintypes.HICON),
                ]

            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            kernel32 = ctypes.windll.kernel32

            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.DefWindowProcW.restype = wintypes.LPARAM
            user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
            user32.RegisterClassW.restype = wintypes.ATOM
            user32.CreateWindowExW.argtypes = [
                wintypes.DWORD,
                wintypes.LPCWSTR,
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.HWND,
                wintypes.HMENU,
                wintypes.HINSTANCE,
                ctypes.c_void_p,
            ]
            user32.CreateWindowExW.restype = wintypes.HWND
            user32.DestroyWindow.argtypes = [wintypes.HWND]
            user32.DestroyWindow.restype = wintypes.BOOL
            user32.PostQuitMessage.argtypes = [ctypes.c_int]
            user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
            user32.GetMessageW.restype = wintypes.BOOL
            user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
            user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
            user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.PostMessageW.restype = wintypes.BOOL
            user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
            user32.GetCursorPos.restype = wintypes.BOOL
            user32.CreatePopupMenu.restype = wintypes.HMENU
            user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR]
            user32.AppendMenuW.restype = wintypes.BOOL
            user32.TrackPopupMenu.argtypes = [
                wintypes.HMENU,
                wintypes.UINT,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.HWND,
                ctypes.c_void_p,
            ]
            user32.TrackPopupMenu.restype = wintypes.UINT
            user32.DestroyMenu.argtypes = [wintypes.HMENU]
            user32.DestroyMenu.restype = wintypes.BOOL
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            user32.SetForegroundWindow.restype = wintypes.BOOL
            user32.LoadImageW.argtypes = [
                wintypes.HINSTANCE,
                wintypes.LPCWSTR,
                wintypes.UINT,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            user32.LoadImageW.restype = wintypes.HANDLE
            user32.LoadIconW.restype = wintypes.HICON
            user32.DestroyIcon.argtypes = [wintypes.HICON]
            user32.DestroyIcon.restype = wintypes.BOOL
            shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
            shell32.Shell_NotifyIconW.restype = wintypes.BOOL

            def delete_icon() -> None:
                try:
                    if self.added and self._nid is not None:
                        shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(self._nid))
                except Exception:
                    pass
                self.added = False

            def show_menu(hwnd: Any) -> None:
                point = POINT()
                if not user32.GetCursorPos(ctypes.byref(point)):
                    return
                menu = user32.CreatePopupMenu()
                if not menu:
                    return
                try:
                    user32.AppendMenuW(menu, self.MF_STRING, self.MENU_FULL, "전체 HUD로 보기")
                    user32.AppendMenuW(menu, self.MF_STRING, self.MENU_COMPACT, "미니 HUD로 보기")
                    user32.AppendMenuW(menu, self.MF_SEPARATOR, 0, None)
                    user32.AppendMenuW(menu, self.MF_STRING, self.MENU_EXIT, "종료")
                    user32.SetForegroundWindow(hwnd)
                    command = int(
                        user32.TrackPopupMenu(
                            menu,
                            self.TPM_RIGHTBUTTON | self.TPM_RETURNCMD,
                            int(point.x),
                            int(point.y),
                            0,
                            hwnd,
                            None,
                        )
                    )
                    user32.PostMessageW(hwnd, self.WM_NULL, 0, 0)
                finally:
                    user32.DestroyMenu(menu)
                if command == self.MENU_FULL:
                    self._commands.put("full")
                elif command == self.MENU_COMPACT:
                    self._commands.put("compact")
                elif command == self.MENU_EXIT:
                    self._commands.put("exit")

            def wndproc(hwnd: Any, msg: int, wparam: Any, lparam: Any) -> Any:
                if msg == self.WM_TRAY and int(wparam) == 1:
                    mouse_msg = int(lparam) & 0xFFFF
                    if mouse_msg in (self.WM_LBUTTONUP, self.WM_LBUTTONDBLCLK):
                        self._commands.put("toggle")
                        return 0
                    if mouse_msg in (self.WM_RBUTTONUP, self.WM_CONTEXTMENU):
                        show_menu(hwnd)
                        return 0
                if msg == self.WM_CONTEXTMENU:
                    show_menu(hwnd)
                    return 0
                if msg == self.WM_TRAY_EXIT:
                    delete_icon()
                    user32.DestroyWindow(hwnd)
                    user32.PostQuitMessage(0)
                    return 0
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            wndproc_type = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            self._wndproc_ref = wndproc_type(wndproc)

            hinstance = kernel32.GetModuleHandleW(None)
            class_name = f"AIUsageMonitorTrayWindow{os.getpid()}"
            wc = WNDCLASSW()
            wc.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p).value
            wc.hInstance = hinstance
            wc.lpszClassName = class_name
            if not user32.RegisterClassW(ctypes.byref(wc)):
                self._ready.set()
                return

            hwnd = user32.CreateWindowExW(0, class_name, APP_TITLE, 0, 0, 0, 0, 0, None, None, hinstance, None)
            if not hwnd:
                self._ready.set()
                return
            self.hwnd = int(hwnd)

            self.hicon = int(
                user32.LoadImageW(None, str(self.icon_path), self.IMAGE_ICON, 0, 0, self.LR_LOADFROMFILE)
            )
            if not self.hicon:
                self.hicon = int(user32.LoadIconW(None, ctypes.c_void_p(32512)))

            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
            nid.uCallbackMessage = self.WM_TRAY
            nid.hIcon = self.hicon
            nid.szTip = self.tooltip
            self._nid = nid
            self.added = bool(shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(nid)))
            self._ready.set()

            msg = MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

            delete_icon()
            if self.hicon:
                try:
                    user32.DestroyIcon(self.hicon)
                except Exception:
                    pass
                self.hicon = None
        except Exception:
            self._ready.set()
            self.added = False

    def is_visible(self) -> bool:
        try:
            return self.root.state() != "withdrawn" and bool(self.root.winfo_viewable())
        except Exception:
            return False

    def show_window(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
            if bool(self.root.attributes("-topmost")):
                self.root.attributes("-topmost", True)
        except Exception:
            pass

    def toggle_window(self) -> None:
        try:
            if self.is_visible():
                self.root.withdraw()
            else:
                self.show_window()
        except Exception:
            pass

    def process_commands(self) -> None:
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            if command == "toggle":
                self.toggle_window()
            elif command == "full" and self.on_show_full is not None:
                self.on_show_full()
            elif command == "compact" and self.on_show_compact is not None:
                self.on_show_compact()
            elif command == "exit" and self.on_exit is not None:
                self.on_exit()

    def remove(self) -> None:
        if not self.enabled:
            return
        try:
            if self.hwnd:
                ctypes.windll.user32.PostMessageW(self.hwnd, self.WM_TRAY_EXIT, 0, 0)
        except Exception:
            pass
        if self._thread is not None and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.0)


def run_gui(topmost: bool = True) -> None:
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:
        print(f"tkinter is not available: {exc}", file=sys.stderr)
        sys.exit(2)

    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("306x214")
    root.minsize(282, 198)
    root.configure(bg="#080b12")
    if topmost:
        root.attributes("-topmost", True)
    root.attributes("-alpha", 0.94)

    icon_images: dict[str, Any] = {}
    if APP_ICON_ICO.exists():
        try:
            root.iconbitmap(str(APP_ICON_ICO))
        except Exception:
            pass
    titlebar_icon_path = APP_TITLEBAR_ICON_PNG if APP_TITLEBAR_ICON_PNG.exists() else APP_ICON_PNG
    if titlebar_icon_path.exists():
        try:
            app_icon = tk.PhotoImage(file=str(titlebar_icon_path))
            root.iconphoto(True, app_icon)
            icon_images["app"] = app_icon
        except Exception:
            pass
    if REFRESH_ICON_PNG.exists():
        try:
            refresh_icon = tk.PhotoImage(file=str(REFRESH_ICON_PNG))
            factor = max(1, min(refresh_icon.width(), refresh_icon.height()) // 24)
            icon_images["refresh"] = refresh_icon.subsample(factor, factor)
        except Exception:
            pass

    for provider in ("codex", "claude"):
        icon_path = find_installed_app_icon(provider)
        if icon_path is None:
            continue
        try:
            original = tk.PhotoImage(file=str(icon_path))
            factor = max(1, min(original.width(), original.height()) // 26)
            icon_images[provider] = original.subsample(factor, factor)
            compact_factor = max(1, min(original.width(), original.height()) // 20)
            icon_images[f"{provider}_compact"] = original.subsample(compact_factor, compact_factor)
        except Exception:
            continue
    if "app" not in icon_images and "codex" in icon_images:
        try:
            root.iconphoto(True, icon_images["codex"])
        except Exception:
            pass

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("Hud.Horizontal.TProgressbar", troughcolor="#202736", bordercolor="#202736", background="#67e8d0")
    style.configure("Hud.Horizontal.TScale", background="#080b12", troughcolor="#1a2130")

    full_geometry = "306x214"
    compact_geometry = "206x58"

    header = tk.Frame(root, bg="#080b12")
    header.pack(fill="x", padx=12, pady=(10, 4))

    title = tk.Label(
        header,
        text="Usage",
        fg="#f7f8fb",
        bg="#080b12",
        font=("Segoe UI Variable Display", 13, "bold"),
    )
    title.pack(side="left")

    status_label = tk.Label(
        header,
        text="refreshing...",
        fg="#8aa2c8",
        bg="#080b12",
        font=("Segoe UI", 8),
    )
    status_label.pack(side="right", pady=(2, 0))

    rows = tk.Frame(root, bg="#080b12")
    rows.pack(fill="both", expand=True, padx=8, pady=(0, 2))

    footer = tk.Frame(root, bg="#080b12")
    footer.pack(fill="x", padx=10, pady=(0, 8))

    scheduled_refresh: str | None = None
    scheduled_topmost: str | None = None
    last_usages: list[ProviderUsage] = []

    top_var = tk.BooleanVar(value=topmost)
    opacity_var = tk.DoubleVar(value=94)
    compact_var = tk.BooleanVar(value=False)

    compact_frame = tk.Frame(root, bg="#080b12")
    compact_canvas = tk.Canvas(compact_frame, height=54, bg="#080b12", highlightthickness=0, cursor="hand2")
    compact_canvas.pack(fill="both", expand=True, padx=6, pady=6)
    compact_drag_start: tuple[int, int] | None = None

    def force_topmost() -> None:
        if not bool(top_var.get()):
            return
        try:
            if root.state() == "withdrawn":
                return
        except Exception:
            return
        try:
            root.attributes("-topmost", True)
            if sys.platform == "win32":
                hwnd = int(root.winfo_id())
                hwnd_topmost = ctypes.c_void_p(-1)
                flags = 0x0001 | 0x0002 | 0x0010 | 0x0040  # NOSIZE | NOMOVE | NOACTIVATE | SHOWWINDOW
                ctypes.windll.user32.SetWindowPos(hwnd, hwnd_topmost, 0, 0, 0, 0, flags)
            else:
                root.lift()
        except Exception:
            pass

    def schedule_topmost_enforcer() -> None:
        nonlocal scheduled_topmost
        if scheduled_topmost is not None:
            try:
                root.after_cancel(scheduled_topmost)
            except Exception:
                pass
            scheduled_topmost = None
        if not bool(top_var.get()):
            return

        def tick() -> None:
            nonlocal scheduled_topmost
            force_topmost()
            scheduled_topmost = root.after(TOPMOST_ENFORCE_MS, tick)

        scheduled_topmost = root.after(TOPMOST_ENFORCE_MS, tick)

    def footer_rounded_rect(
        canvas: tk.Canvas,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        radius: int,
        fill: str,
        outline: str | None = None,
    ) -> None:
        outline_color = outline or fill
        points = [
            x1 + radius,
            y1,
            x2 - radius,
            y1,
            x2,
            y1,
            x2,
            y1 + radius,
            x2,
            y2 - radius,
            x2,
            y2,
            x2 - radius,
            y2,
            x1 + radius,
            y2,
            x1,
            y2,
            x1,
            y2 - radius,
            x1,
            y1 + radius,
            x1,
            y1,
        ]
        canvas.create_polygon(points, fill=fill, outline=outline_color, smooth=True, splinesteps=12)

    def toggle_topmost() -> None:
        next_value = not bool(top_var.get())
        top_var.set(next_value)
        root.attributes("-topmost", next_value)
        if next_value:
            force_topmost()
        schedule_topmost_enforcer()
        draw_footer()

    def set_opacity(value: str) -> None:
        try:
            alpha = max(0.45, min(1.0, float(value) / 100))
            opacity_var.set(alpha * 100)
            root.attributes("-alpha", alpha)
            draw_footer()
        except Exception:
            pass

    def set_compact_mode(value: bool) -> None:
        compact_var.set(value)
        if value:
            header.pack_forget()
            rows.pack_forget()
            footer.pack_forget()
            root.overrideredirect(True)
            compact_frame.pack(fill="both", expand=True)
            root.minsize(194, 54)
            root.geometry(compact_geometry)
            render_compact()
            root.after(80, force_topmost)
        else:
            compact_frame.pack_forget()
            root.overrideredirect(False)
            header.pack(fill="x", padx=12, pady=(10, 4))
            rows.pack(fill="both", expand=True, padx=8, pady=(0, 2))
            footer.pack(fill="x", padx=10, pady=(0, 8))
            root.minsize(282, 198)
            root.geometry(full_geometry)
            clear_rows()
            if last_usages:
                render_modern_provider(rows, last_usages[0], "#67e8d0", "Cx")
                if len(last_usages) > 1:
                    render_modern_provider(rows, last_usages[1], "#ff8a5c", "Cl")
            draw_footer()
            root.after(80, force_topmost)

    footer_canvas = tk.Canvas(footer, height=28, bg="#080b12", highlightthickness=0, cursor="hand2")
    footer_canvas.pack(fill="x", expand=True)
    dragging_opacity = False

    def footer_regions(width: int) -> dict[str, tuple[int, int, int, int]]:
        return {
            "pin": (0, 2, 46, 26),
            "slider": (56, 2, max(136, width - 82), 26),
            "compact": (width - 68, 2, width - 36, 26),
            "refresh": (width - 30, 2, width - 2, 26),
        }

    def draw_refresh_icon(canvas: tk.Canvas, cx: int, cy: int) -> None:
        canvas.create_arc(cx - 7, cy - 7, cx + 7, cy + 7, start=35, extent=285, style="arc", outline="#e9fffb", width=2)
        canvas.create_polygon(cx + 6, cy - 8, cx + 11, cy - 7, cx + 8, cy - 3, fill="#e9fffb", outline="#e9fffb")

    def draw_footer(event: Any | None = None) -> None:
        footer_canvas.delete("all")
        width = event.width if event is not None else max(footer_canvas.winfo_width(), 280)
        regions = footer_regions(width)

        pin = regions["pin"]
        pin_on = bool(top_var.get())
        footer_rounded_rect(
            footer_canvas,
            *pin,
            radius=7,
            fill="#12352f" if pin_on else "#121a27",
            outline="#1d8f83" if pin_on else "#263247",
        )
        footer_canvas.create_text(
            (pin[0] + pin[2]) // 2,
            14,
            text="PIN",
            fill="#dffff9" if pin_on else "#a9b4c5",
            font=("Segoe UI Semibold", 8),
        )

        slider = regions["slider"]
        track_x1 = slider[0]
        track_x2 = slider[2] - 38
        track_y = 14
        footer_canvas.create_line(track_x1, track_y, track_x2, track_y, fill="#2a3345", width=4, capstyle="round")
        pct = max(45.0, min(100.0, float(opacity_var.get())))
        knob_x = int(track_x1 + (track_x2 - track_x1) * (pct - 45.0) / 55.0)
        footer_canvas.create_line(track_x1, track_y, knob_x, track_y, fill="#67e8d0", width=4, capstyle="round")
        footer_canvas.create_oval(knob_x - 5, track_y - 5, knob_x + 5, track_y + 5, fill="#eafffb", outline="#67e8d0", width=1)
        footer_canvas.create_text(slider[2], 14, text=f"{pct:.0f}%", fill="#b7c5dc", anchor="e", font=("Segoe UI", 8))

        compact_region = regions["compact"]
        footer_rounded_rect(footer_canvas, *compact_region, radius=7, fill="#121a27", outline="#263247")
        footer_canvas.create_text(
            (compact_region[0] + compact_region[2]) // 2,
            14,
            text="MIN",
            fill="#a9b4c5",
            font=("Segoe UI Semibold", 7),
        )

        refresh_region = regions["refresh"]
        refresh_icon = icon_images.get("refresh")
        if refresh_icon is not None:
            footer_canvas.create_image((refresh_region[0] + refresh_region[2]) // 2, 14, image=refresh_icon)
        else:
            draw_refresh_icon(footer_canvas, (refresh_region[0] + refresh_region[2]) // 2, 14)

    def update_opacity_from_x(x: int) -> None:
        width = max(footer_canvas.winfo_width(), 280)
        slider = footer_regions(width)["slider"]
        track_x1 = slider[0]
        track_x2 = slider[2] - 38
        ratio = 0.0 if track_x2 <= track_x1 else (x - track_x1) / (track_x2 - track_x1)
        set_opacity(str(45.0 + max(0.0, min(1.0, ratio)) * 55.0))

    def footer_press(event: Any) -> None:
        nonlocal dragging_opacity
        width = max(footer_canvas.winfo_width(), 280)
        regions = footer_regions(width)
        x, y = event.x, event.y
        if regions["pin"][0] <= x <= regions["pin"][2] and regions["pin"][1] <= y <= regions["pin"][3]:
            toggle_topmost()
            return
        if regions["compact"][0] <= x <= regions["compact"][2] and regions["compact"][1] <= y <= regions["compact"][3]:
            set_compact_mode(True)
            return
        if regions["refresh"][0] <= x <= regions["refresh"][2] and regions["refresh"][1] <= y <= regions["refresh"][3]:
            refresh()
            return
        if regions["slider"][0] <= x <= regions["slider"][2] and regions["slider"][1] <= y <= regions["slider"][3]:
            dragging_opacity = True
            update_opacity_from_x(x)

    def footer_drag(event: Any) -> None:
        if dragging_opacity:
            update_opacity_from_x(event.x)

    def footer_release(event: Any) -> None:
        nonlocal dragging_opacity
        dragging_opacity = False

    footer_canvas.bind("<Configure>", draw_footer)
    footer_canvas.bind("<Button-1>", footer_press)
    footer_canvas.bind("<B1-Motion>", footer_drag)
    footer_canvas.bind("<ButtonRelease-1>", footer_release)
    draw_footer()
    def compact_press(event: Any) -> None:
        nonlocal compact_drag_start
        compact_drag_start = (event.x_root, event.y_root)

    def compact_drag(event: Any) -> None:
        nonlocal compact_drag_start
        if compact_drag_start is None:
            return
        start_x, start_y = compact_drag_start
        dx = event.x_root - start_x
        dy = event.y_root - start_y
        if abs(dx) + abs(dy) < 2:
            return
        root.geometry(f"+{root.winfo_x() + dx}+{root.winfo_y() + dy}")
        compact_drag_start = (event.x_root, event.y_root)

    def compact_release(event: Any) -> None:
        nonlocal compact_drag_start
        compact_drag_start = None
        force_topmost()

    compact_canvas.bind("<Button-1>", compact_press)
    compact_canvas.bind("<Double-Button-1>", lambda event: set_compact_mode(False))
    compact_canvas.bind("<B1-Motion>", compact_drag)
    compact_canvas.bind("<ButtonRelease-1>", compact_release)
    root.bind("<Map>", lambda event: root.after(80, force_topmost))
    root.bind("<FocusIn>", lambda event: force_topmost())

    def rounded_rect(canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int, radius: int, fill: str) -> None:
        canvas.create_rectangle(x1 + radius, y1, x2 - radius, y2, fill=fill, outline=fill)
        canvas.create_rectangle(x1, y1 + radius, x2, y2 - radius, fill=fill, outline=fill)
        canvas.create_oval(x1, y1, x1 + radius * 2, y1 + radius * 2, fill=fill, outline=fill)
        canvas.create_oval(x2 - radius * 2, y1, x2, y1 + radius * 2, fill=fill, outline=fill)
        canvas.create_oval(x1, y2 - radius * 2, x1 + radius * 2, y2, fill=fill, outline=fill)
        canvas.create_oval(x2 - radius * 2, y2 - radius * 2, x2, y2, fill=fill, outline=fill)

    def draw_progress(canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int, value: float | None, fill: str) -> None:
        rounded_rect(canvas, x1, y1, x2, y2, 3, "#202736")
        if value is None:
            return
        fill_x = int(x1 + (x2 - x1) * max(0.0, min(100.0, value)) / 100)
        if fill_x > x1:
            rounded_rect(canvas, x1, y1, fill_x, y2, 3, fill)

    def clear_rows() -> None:
        for child in rows.winfo_children():
            child.destroy()

    def primary_metric(usage: ProviderUsage) -> UsageMetric | None:
        if not usage.metrics:
            return None
        if usage.provider == "codex":
            return usage.metrics[0]
        for metric in usage.metrics:
            if metric.label.lower().startswith(("5", "current", "all")):
                return metric
        return usage.metrics[0]

    def secondary_summary(usage: ProviderUsage, metric: UsageMetric | None) -> str:
        if usage.status == "stale":
            return "stale"
        if metric and metric.reset_at:
            return reset_text(metric.reset_at).replace("resets in ", "")
        if usage.provider == "claude" and len(usage.metrics) > 1:
            return " · ".join(f"{m.label[:1]} {percent_text(m.used_percent)}" for m in usage.metrics[1:3])
        return usage.confidence

    def render_provider(parent: tk.Frame, usage: ProviderUsage, accent: str, badge: str) -> None:
        row = tk.Frame(parent, bg="#151923", highlightbackground="#252a35", highlightthickness=1)
        row.pack(fill="x", pady=3)

        left = tk.Frame(row, bg="#151923")
        left.pack(fill="x", padx=8, pady=(5, 1))

        icon_image = icon_images.get(usage.provider)
        if icon_image is not None:
            badge_label = tk.Label(left, image=icon_image, bg="#151923", width=24, height=24)
        else:
            badge_label = tk.Label(
                left,
                text=badge,
                fg="#111318",
                bg=accent,
                font=("Segoe UI Semibold", 8),
                width=2,
                height=1,
            )
        badge_label.pack(side="left")

        metric = primary_metric(usage)
        display_name = "Codex" if usage.provider == "codex" else "Claude"
        name = tk.Label(
            left,
            text=display_name,
            fg="#f3f5f7",
            bg="#151923",
            font=("Segoe UI Semibold", 9),
        )
        name.pack(side="left", padx=(7, 0))

        pct = percent_text(metric.used_percent if metric else None)
        pct_label = tk.Label(
            left,
            text=pct,
            fg="#f3f5f7",
            bg="#151923",
            font=("Segoe UI Semibold", 10),
        )
        pct_label.pack(side="right")

        status = tk.Label(
            left,
            text=secondary_summary(usage, metric),
            fg=status_color(usage.status),
            bg="#151923",
            font=("Segoe UI", 8),
        )
        status.pack(side="right", padx=(0, 8))

        bar = ttk.Progressbar(
            row,
            maximum=100,
            value=metric.used_percent if metric and metric.used_percent is not None else 0,
            mode="determinate",
            style="Hud.Horizontal.TProgressbar",
        )
        bar.pack(fill="x", padx=8, pady=(0, 5))

    def render_modern_provider(parent: tk.Frame, usage: ProviderUsage, accent: str, badge: str) -> None:
        metric = primary_metric(usage)
        display_name = "Codex" if usage.provider == "codex" else "Claude"
        canvas = tk.Canvas(parent, height=58, bg="#080b12", highlightthickness=0)
        canvas.pack(fill="x", pady=2)
        icon_image = icon_images.get(usage.provider)

        def summary_text() -> str:
            if usage.status == "stale":
                return "stale cache"
            if metric and metric.reset_at:
                return reset_text(metric.reset_at).replace("resets in ", "")
            if usage.provider == "claude" and len(usage.metrics) > 1:
                return " / ".join(f"{m.label[:1]} {percent_text(m.used_percent)}" for m in usage.metrics[1:3])
            return usage.confidence

        def redraw(event: Any | None = None) -> None:
            canvas.delete("all")
            width = event.width if event is not None else max(canvas.winfo_width(), 260)
            pct_value = metric.used_percent if metric else None
            metric_color = usage_alert_color(pct_value, accent)
            rounded_rect(canvas, 0, 0, width, 58, 14, "#111722")
            if icon_image is not None:
                canvas.create_image(26, 29, image=icon_image)
            else:
                rounded_rect(canvas, 13, 16, 39, 42, 7, accent)
                canvas.create_text(26, 29, text=badge, fill="#071014", font=("Segoe UI Semibold", 8))
            canvas.create_text(52, 17, text=display_name, fill="#f7f8fb", anchor="w", font=("Segoe UI Semibold", 9))
            canvas.create_text(
                52,
                36,
                text=summary_text(),
                fill=status_color(usage.status),
                anchor="w",
                font=("Segoe UI", 8),
            )
            bar_x1 = 112
            bar_x2 = width - 58
            if bar_x2 > bar_x1 + 24:
                draw_progress(canvas, bar_x1, 20, bar_x2, 25, pct_value, metric_color)
            canvas.create_text(
                width - 15,
                22,
                text=percent_text(pct_value),
                fill=metric_color,
                anchor="e",
                font=("Segoe UI Variable Display", 11, "bold"),
            )
            confidence_text = usage.confidence if usage.status == "ok" else usage.status
            canvas.create_text(width - 15, 38, text=confidence_text, fill="#69758a", anchor="e", font=("Segoe UI", 7))

        canvas.bind("<Configure>", redraw)
        redraw()

    def render_compact(event: Any | None = None) -> None:
        compact_canvas.delete("all")
        width = event.width if event is not None else max(compact_canvas.winfo_width(), 194)
        height = event.height if event is not None else max(compact_canvas.winfo_height(), 46)
        if not last_usages:
            rounded_rect(compact_canvas, 0, 0, width, height, 14, "#111722")
            compact_canvas.create_text(width // 2, height // 2, text="refreshing...", fill="#8aa2c8", font=("Segoe UI", 9))
            return

        codex = last_usages[0]
        claude = last_usages[1] if len(last_usages) > 1 else None
        codex_metric = primary_metric(codex)
        claude_metric = primary_metric(claude) if claude is not None else None
        codex_color = usage_alert_color(codex_metric.used_percent if codex_metric else None, "#67e8d0")
        claude_color = usage_alert_color(claude_metric.used_percent if claude_metric else None, "#ff8a5c")

        rounded_rect(compact_canvas, 0, 0, width, height, 14, "#111722")

        mid = 104
        codex_icon = icon_images.get("codex_compact") or icon_images.get("codex")
        claude_icon = icon_images.get("claude_compact") or icon_images.get("claude")
        cy = height // 2

        if codex_icon is not None:
            compact_canvas.create_image(25, cy, image=codex_icon)
        else:
            compact_canvas.create_text(25, cy, text="Cx", fill="#67e8d0", font=("Segoe UI Semibold", 10))
        compact_canvas.create_text(
            45,
            cy,
            text=percent_text(codex_metric.used_percent if codex_metric else None),
            fill=codex_color,
            anchor="w",
            font=("Segoe UI Variable Display", 17, "bold"),
        )

        if claude_icon is not None:
            compact_canvas.create_image(mid + 16, cy, image=claude_icon)
        else:
            compact_canvas.create_text(mid + 16, cy, text="Cl", fill="#ff8a5c", font=("Segoe UI Semibold", 10))
        compact_canvas.create_text(
            mid + 32,
            cy,
            text=percent_text(claude_metric.used_percent if claude_metric else None),
            fill=claude_color,
            anchor="w",
            font=("Segoe UI Variable Display", 17, "bold"),
        )

    def refresh(schedule_next: bool = True) -> None:
        nonlocal scheduled_refresh, last_usages
        if scheduled_refresh is not None:
            try:
                root.after_cancel(scheduled_refresh)
            except Exception:
                pass
            scheduled_refresh = None
        clear_rows()
        usages = collect_all()
        last_usages = usages
        if compact_var.get():
            render_compact()
        else:
            render_modern_provider(rows, usages[0], "#67e8d0", "Cx")
            render_modern_provider(rows, usages[1], "#ff8a5c", "Cl")
        status_label.config(text=datetime.now().strftime("%H:%M:%S"))
        if schedule_next:
            scheduled_refresh = root.after(REFRESH_MS, refresh)

    compact_canvas.bind("<Configure>", render_compact)
    tray_icon: WindowsTrayIcon | None = None

    def close_app() -> None:
        if scheduled_refresh is not None:
            try:
                root.after_cancel(scheduled_refresh)
            except Exception:
                pass
        if scheduled_topmost is not None:
            try:
                root.after_cancel(scheduled_topmost)
            except Exception:
                pass
        if tray_icon is not None:
            tray_icon.remove()
        root.destroy()

    def show_full_from_tray() -> None:
        root.deiconify()
        set_compact_mode(False)
        root.after(80, force_topmost)

    def show_compact_from_tray() -> None:
        root.deiconify()
        set_compact_mode(True)
        root.after(80, force_topmost)

    root.protocol("WM_DELETE_WINDOW", close_app)
    root.update_idletasks()
    tray_icon = WindowsTrayIcon(
        root,
        APP_ICON_ICO if APP_ICON_ICO.exists() else titlebar_icon_path,
        on_show_full=show_full_from_tray,
        on_show_compact=show_compact_from_tray,
        on_exit=close_app,
    )
    tray_icon.install()

    def process_tray_commands() -> None:
        if tray_icon is not None:
            tray_icon.process_commands()
        try:
            root.after(120, process_tray_commands)
        except Exception:
            pass

    process_tray_commands()
    schedule_topmost_enforcer()
    force_topmost()
    refresh()
    try:
        root.mainloop()
    finally:
        tray_icon.remove()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=APP_TITLE)
    parser.add_argument("--once", action="store_true", help="Print one collector snapshot and exit.")
    parser.add_argument("--json", action="store_true", help="Use JSON output with --once.")
    parser.add_argument("--no-topmost", action="store_true", help="Do not keep the monitor above other windows.")
    args = parser.parse_args(argv)

    if args.once:
        usages = collect_all()
        if args.json:
            print(json.dumps([u.to_jsonable() for u in usages], ensure_ascii=False, indent=2))
        else:
            for usage in usages:
                print(f"{usage.display_name} [{usage.status}, {usage.confidence}]")
                if usage.message:
                    print(f"  {usage.message}")
                for metric in usage.metrics:
                    print(f"  {metric.label}: {percent_text(metric.used_percent)} ({reset_text(metric.reset_at)})")
                print(f"  source: {usage.source}")
        return 0

    run_gui(topmost=not args.no_topmost)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
