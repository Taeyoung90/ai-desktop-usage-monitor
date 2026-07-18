from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_TITLE = "AI Usage Monitor"
REFRESH_MS = 30_000
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
    last_usages: list[ProviderUsage] = []

    top_var = tk.BooleanVar(value=topmost)
    opacity_var = tk.DoubleVar(value=94)
    compact_var = tk.BooleanVar(value=False)

    compact_frame = tk.Frame(root, bg="#080b12")
    compact_canvas = tk.Canvas(compact_frame, height=54, bg="#080b12", highlightthickness=0, cursor="hand2")
    compact_canvas.pack(fill="both", expand=True, padx=6, pady=6)
    compact_drag_start: tuple[int, int] | None = None

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

    compact_canvas.bind("<Button-1>", compact_press)
    compact_canvas.bind("<Double-Button-1>", lambda event: set_compact_mode(False))
    compact_canvas.bind("<B1-Motion>", compact_drag)
    compact_canvas.bind("<ButtonRelease-1>", compact_release)

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
    refresh()
    root.mainloop()


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
