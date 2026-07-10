from __future__ import annotations

import argparse
import ctypes
import io
import json
import os
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audio_routing import (
    audio_file_info,
    begin_native_voice_audio_route,
    diagnose_all_native_voice_loopbacks,
    diagnose_native_voice_loopback,
    native_voice_route_status,
    play_prepared_portaudio,
    play_wav_to_portaudio_device,
    play_wav_to_render_endpoint,
    prepare_wav_for_portaudio_device,
    prewarm_portaudio_device,
    restore_native_voice_audio_route,
    select_portaudio_device,
    stop_sounddevice_playback,
)
from .policy import chat_is_allowed, looks_like_stable_chat_id, normalize_allowed_chats


COMPOSE_X = 0.53
COMPOSE_Y = 0.91
SEARCH_X = 0.155
SEARCH_Y = 0.064
SEARCH_RESULT_X = 0.16
SEARCH_RESULT_Y = 0.145
SEND_BUTTON_X = 0.957
SEND_BUTTON_Y = 0.963
SEND_BUTTON_FALLBACK_POINTS = (
    (SEND_BUTTON_X, SEND_BUTTON_Y),
    (0.945, 0.963),
    (0.967, 0.963),
    (0.957, 0.948),
)
def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        return float(raw) if raw else float(default)
    except (TypeError, ValueError):
        return float(default)


# WeChat 4.x compose-bar voice buttons. Defaults corrected from the originally
# shipped values (0.893/0.789/0.957) after live measurement on 2026-06-16; the
# toolbar is right-anchored so the fraction drifts with window width/DPI, hence
# the WECHAT_PC_VOICE_* env overrides for per-machine calibration without edits.
# Left mic icon (~0.52) is voice-to-text dictation, NOT this; the round icon next
# to 发送 (~0.863) is 发语音. Recording mode then shows ✕ cancel / green ↑ send.
VOICE_START_BUTTON_X = _env_float("WECHAT_PC_VOICE_START_X", 0.863)
VOICE_START_BUTTON_Y = _env_float("WECHAT_PC_VOICE_START_Y", 0.955)
VOICE_CANCEL_BUTTON_X = _env_float("WECHAT_PC_VOICE_CANCEL_X", 0.734)
VOICE_CANCEL_BUTTON_Y = _env_float("WECHAT_PC_VOICE_CANCEL_Y", 0.955)
VOICE_SEND_BUTTON_X = _env_float("WECHAT_PC_VOICE_SEND_X", 0.947)
VOICE_SEND_BUTTON_Y = _env_float("WECHAT_PC_VOICE_SEND_Y", 0.955)
VOICE_DEFAULT_RECORD_SECONDS = 2.0
VOICE_MIN_RECORD_SECONDS = 1.2
VOICE_MAX_RECORD_SECONDS = 55.0
VOICE_TTS_PLAYBACK_DELAY_SECONDS = 0.08
VOICE_TTS_RECORD_PADDING_SECONDS = 0.25
MEDIA_ACTION_TYPES = {"image", "animated_sticker"}
LOGIN_REQUIRED_WINDOW_CLASSES = {"mmui::LoginWindow"}
LOGIN_REQUIRED_CLASS_KEYWORDS = ("LoginWindow",)
LOGIN_REQUIRED_SMALL_WINDOW_MAX_AREA = 180_000
LOGIN_REQUIRED_SMALL_WINDOW_MAX_EDGE = 520
RECENT_CONTACT_SWITCH_TTL_SECONDS = 12.0
CF_DIB = 8
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002
DROPEFFECT_COPY = 1
MEDIA_SEND_STATUS_MAX_CHECKS = 60
MEDIA_SEND_STATUS_POLL_SECONDS = 0.5
WECHAT_PROCESS_NAMES = {"wechat.exe", "weixin.exe"}


class BridgeError(RuntimeError):
    pass


def print_json(payload: Any, *, indent: int | None = None, file: Any | None = None) -> None:
    target = file or sys.stdout
    try:
        print(json.dumps(payload, ensure_ascii=False, indent=indent), file=target, flush=True)
    except UnicodeEncodeError:
        print(json.dumps(payload, ensure_ascii=True, indent=indent), file=target, flush=True)


def native_window_pid(hwnd: int) -> int:
    if not hwnd:
        return 0
    try:
        pid = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
        return int(pid.value or 0)
    except Exception:
        return 0


def process_identity_matches_wechat(name: str = "", path: str = "") -> bool:
    raw_name = str(name or "").strip()
    raw_path = str(path or "").strip()
    base = os.path.basename(raw_path or raw_name).lower()
    if base in WECHAT_PROCESS_NAMES:
        return True
    lowered = raw_path.replace("/", "\\").lower()
    return lowered.endswith("\\tencent\\weixin\\weixin.exe") or lowered.endswith("\\tencent\\wechat\\wechat.exe")


def native_window_process_identity(hwnd: int) -> dict[str, Any]:
    pid = native_window_pid(hwnd)
    payload: dict[str, Any] = {"pid": pid, "name": "", "path": "", "matches_wechat": False}
    if not pid:
        return payload
    try:
        import psutil  # type: ignore

        proc = psutil.Process(pid)
        payload["name"] = str(proc.name() or "")
        payload["path"] = str(proc.exe() or "")
    except Exception:
        pass
    if not payload.get("path"):
        try:
            from ctypes import wintypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if handle:
                try:
                    size = wintypes.DWORD(32768)
                    buffer = ctypes.create_unicode_buffer(size.value)
                    if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                        payload["path"] = str(buffer.value or "")
                finally:
                    kernel32.CloseHandle(handle)
        except Exception:
            pass
    if not payload.get("name") and payload.get("path"):
        payload["name"] = os.path.basename(str(payload.get("path") or ""))
    payload["matches_wechat"] = process_identity_matches_wechat(
        str(payload.get("name") or ""),
        str(payload.get("path") or ""),
    )
    return payload


def is_login_required_window_class(class_name: str) -> bool:
    raw = str(class_name or "")
    return raw in LOGIN_REQUIRED_WINDOW_CLASSES or any(keyword in raw for keyword in LOGIN_REQUIRED_CLASS_KEYWORDS)


def is_probable_login_required_window(
    *,
    title: str = "",
    class_name: str = "",
    width: int = 0,
    height: int = 0,
) -> bool:
    if is_login_required_window_class(class_name):
        return True
    title = str(title or "").strip()
    if title not in {"微信", "WeChat", "Weixin"}:
        return False
    width = max(0, int(width or 0))
    height = max(0, int(height or 0))
    if not width or not height:
        return False
    area = width * height
    return area <= LOGIN_REQUIRED_SMALL_WINDOW_MAX_AREA and max(width, height) <= LOGIN_REQUIRED_SMALL_WINDOW_MAX_EDGE


def login_required_error(class_name: str) -> str:
    return f"wechat_login_required: PC WeChat is showing a login/security window ({class_name or 'unknown class'})"


def is_login_required_error_text(error: str) -> bool:
    raw = str(error or "")
    return "wechat_login_required" in raw or any(keyword in raw for keyword in LOGIN_REQUIRED_CLASS_KEYWORDS)


def run_hidden_powershell(script: str, *args: str, timeout: int = 10) -> None:
    command = [
        "powershell",
        "-NoProfile",
        "-STA",
        "-WindowStyle",
        "Hidden",
        "-Command",
        script,
        *args,
    ]
    subprocess.run(command, check=True, timeout=timeout)


def _format_win_error(prefix: str) -> str:
    code = ctypes.get_last_error()
    try:
        message = ctypes.FormatError(code).strip()
    except Exception:
        message = ""
    return f"{prefix}: {message or code}"


def _win_clipboard_dlls() -> tuple[Any, Any]:
    if os.name != "nt":
        raise BridgeError("native clipboard requires Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = ctypes.c_bool
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_bool
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
    user32.RegisterClipboardFormatW.restype = ctypes.c_uint
    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p
    return user32, kernel32


def _global_alloc_bytes(kernel32: Any, data: bytes) -> Any:
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        raise BridgeError(_format_win_error("GlobalAlloc failed"))
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        kernel32.GlobalFree(handle)
        raise BridgeError(_format_win_error("GlobalLock failed"))
    try:
        ctypes.memmove(pointer, data, len(data))
    finally:
        kernel32.GlobalUnlock(handle)
    return handle


def _set_native_clipboard_formats(formats: list[tuple[int, bytes]]) -> None:
    user32, kernel32 = _win_clipboard_dlls()
    opened = False
    for attempt in range(10):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.05 + (attempt * 0.03))
    if not opened:
        raise BridgeError(_format_win_error("OpenClipboard failed"))

    try:
        if not user32.EmptyClipboard():
            raise BridgeError(_format_win_error("EmptyClipboard failed"))
        for format_id, data in formats:
            handle = _global_alloc_bytes(kernel32, data)
            if not user32.SetClipboardData(format_id, handle):
                kernel32.GlobalFree(handle)
                raise BridgeError(_format_win_error(f"SetClipboardData({format_id}) failed"))
    finally:
        user32.CloseClipboard()


def _register_clipboard_format(name: str) -> int:
    user32, _kernel32 = _win_clipboard_dlls()
    format_id = int(user32.RegisterClipboardFormatW(name))
    if not format_id:
        raise BridgeError(_format_win_error(f"RegisterClipboardFormat({name}) failed"))
    return format_id


def _image_clipboard_formats(path: Path) -> list[tuple[int, bytes]]:
    try:
        from PIL import Image  # type: ignore
    except Exception as exc:
        raise BridgeError("image clipboard needs Pillow installed") from exc

    with Image.open(path) as raw_image:
        image = raw_image.copy()
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        image = background.convert("RGB")
    else:
        image = image.convert("RGB")

    bmp = io.BytesIO()
    image.save(bmp, "BMP")
    dib = bmp.getvalue()[14:]
    png = io.BytesIO()
    image.save(png, "PNG")
    return [
        (CF_DIB, dib),
        (_register_clipboard_format("PNG"), png.getvalue()),
    ]


def _file_drop_clipboard_formats(path: Path) -> list[tuple[int, bytes]]:
    resolved = str(path.resolve())
    dropfiles_header = struct.pack("<IiiII", 20, 0, 0, 0, 1)
    file_list = f"{resolved}\0\0".encode("utf-16le")
    preferred_drop_effect = _register_clipboard_format("Preferred DropEffect")
    return [
        (CF_HDROP, dropfiles_header + file_list),
        (preferred_drop_effect, struct.pack("<I", DROPEFFECT_COPY)),
    ]


def copy_image_to_clipboard(path: Path) -> None:
    _set_native_clipboard_formats(_image_clipboard_formats(path))


def copy_file_to_clipboard(path: Path) -> None:
    _set_native_clipboard_formats(_file_drop_clipboard_formats(path))


def copy_media_to_clipboard(path: Path, *, preserve_animation: bool = False) -> str:
    if preserve_animation:
        copy_file_to_clipboard(path)
        return "file_drop"
    try:
        copy_image_to_clipboard(path)
        return "bitmap_dib"
    except Exception as image_error:
        try:
            copy_file_to_clipboard(path)
        except Exception as file_error:
            raise BridgeError(
                "clipboard setup failed for both image and file-drop "
                f"(image_error={image_error}; file_error={file_error})"
            ) from file_error
        return "file_drop_fallback"



def rect_payload(rect: Any) -> dict[str, int]:
    if rect is None:
        return {}

    def value(name: str) -> int:
        try:
            return int(getattr(rect, name))
        except Exception:
            return 0

    try:
        width = int(rect.width())
    except Exception:
        width = max(0, value("right") - value("left"))
    try:
        height = int(rect.height())
    except Exception:
        height = max(0, value("bottom") - value("top"))
    return {
        "left": value("left"),
        "top": value("top"),
        "right": value("right"),
        "bottom": value("bottom"),
        "width": width,
        "height": height,
    }


def control_payload(control: Any) -> dict[str, Any]:
    if control is None:
        return {}
    payload: dict[str, Any] = {}
    for attr in ("Name", "ClassName", "ControlTypeName"):
        try:
            key = attr[0].lower() + attr[1:]
            payload[key] = str(getattr(control, attr) or "")
        except Exception:
            payload[key] = ""
    for attr, key in (
        ("IsValuePatternAvailable", "value_pattern_available"),
        ("IsInvokePatternAvailable", "invoke_pattern_available"),
    ):
        try:
            payload[key] = bool(getattr(control, attr, False))
        except Exception:
            payload[key] = False
    try:
        payload["rect"] = rect_payload(getattr(control, "BoundingRectangle", None))
    except Exception:
        payload["rect"] = {}
    return payload


GENERIC_CHAT_TARGET_LABELS = {
    "微信",
    "wechat",
    "weixin",
    "搜索",
    "search",
    "聊天",
    "通讯录",
    "收藏",
    "设置",
    "发送",
    "send",
    "表情",
    "文件",
    "截图",
    "聊天信息",
    "更多",
    "最小化",
    "最大化",
    "关闭",
}

BLOCKED_WECHAT_TARGET_LABELS = {
    "公众号",
    "服务号",
    "订阅号",
    "微信团队",
}

VISUAL_OCR_MIN_CONFIDENCE = 0.35


def normalize_chat_label(value: str) -> str:
    raw = str(value or "").replace("\u200b", "").replace("\ufeff", "").strip()
    return " ".join(raw.split()).casefold()


def chat_label_matches(expected: str, observed: str) -> bool:
    expected_norm = normalize_chat_label(expected)
    observed_norm = normalize_chat_label(observed)
    return bool(expected_norm and observed_norm and expected_norm == observed_norm)


def normalize_visual_chat_label(value: str) -> str:
    normalized = normalize_chat_label(value)
    replacements = {
        "^": "a",
        "ˆ": "a",
        "︿": "a",
        "∧": "a",
        "Λ": "a",
        "λ": "a",
    }
    return "".join(replacements.get(ch, ch) for ch in normalized if not ch.isspace())


def visual_chat_label_matches(expected: str, observed: str) -> bool:
    if chat_label_matches(expected, observed):
        return True
    expected_norm = normalize_visual_chat_label(expected)
    observed_norm = normalize_visual_chat_label(observed)
    return bool(expected_norm and observed_norm and expected_norm == observed_norm)


def is_generic_chat_target_label(value: str) -> bool:
    normalized = normalize_chat_label(value)
    generic = {normalize_chat_label(label) for label in GENERIC_CHAT_TARGET_LABELS}
    return not normalized or normalized in generic


def is_blocked_wechat_target_label(value: str) -> bool:
    normalized = normalize_chat_label(value)
    blocked = {normalize_chat_label(label) for label in BLOCKED_WECHAT_TARGET_LABELS}
    return bool(normalized and normalized in blocked)


def target_chat_mismatch_message(evidence: Any, phase: str = "send") -> str:
    if not isinstance(evidence, dict):
        return ""
    expected = str(evidence.get("expected") or "")
    observed = str(evidence.get("observed") or "")
    if not observed:
        observed_names = evidence.get("observed_names")
        if isinstance(observed_names, list) and observed_names:
            observed = str(observed_names[0] or "")
    return f"target chat mismatch {phase}: expected {expected!r}, observed {observed!r}"


def target_chat_unconfirmed_message(evidence: Any, phase: str = "send") -> str:
    if not isinstance(evidence, dict):
        return f"target chat unconfirmed {phase}: missing target evidence"
    expected = str(evidence.get("expected") or "")
    status = str(evidence.get("status") or "unknown")
    observed = str(evidence.get("observed") or "")
    detail = f", observed {observed!r}" if observed else ""
    blocker_message = str(evidence.get("blocker_message") or "")
    if blocker_message:
        detail = f"{detail}, {blocker_message}"
    return f"target chat unconfirmed {phase}: expected {expected!r}, status {status!r}{detail}"


def target_evidence_confirmed(target_evidence: Any) -> bool:
    if not isinstance(target_evidence, dict):
        return False
    if target_evidence.get("mismatch") is True:
        return False
    if target_evidence.get("matched") is True:
        return True
    for phase in ("before_send", "after_send"):
        phase_evidence = target_evidence.get(phase)
        if isinstance(phase_evidence, dict) and phase_evidence.get("matched") is True:
            return True
    return False


def target_evidence_error(target_evidence: Any) -> str:
    if not isinstance(target_evidence, dict):
        return "target chat unconfirmed send: missing target evidence"
    before_send = target_evidence.get("before_send")
    if isinstance(before_send, dict):
        if before_send.get("mismatch") is True:
            return target_chat_mismatch_message(before_send, "before_send")
        if before_send.get("matched") is True:
            return ""
    checks: list[tuple[str, Any]] = []
    for phase in ("before_send", "after_send"):
        phase_evidence = target_evidence.get(phase)
        if isinstance(phase_evidence, dict):
            checks.append((phase, phase_evidence))
    if not checks:
        checks.append(("send", target_evidence))
    for phase, evidence in checks:
        if isinstance(evidence, dict) and evidence.get("mismatch") is True:
            return target_chat_mismatch_message(evidence, phase)
    if target_evidence.get("mismatch") is True:
        return target_chat_mismatch_message(target_evidence, "send")
    if not target_evidence_confirmed(target_evidence):
        for phase, evidence in checks:
            if isinstance(evidence, dict):
                return target_chat_unconfirmed_message(evidence, phase)
        return target_chat_unconfirmed_message(target_evidence, "send")
    return ""


def _is_wechat_outgoing_green(pixel: Any) -> bool:
    try:
        r, g, b = [int(value) for value in pixel[:3]]
    except Exception:
        return False
    return (
        g >= 120
        and r <= 95
        and b <= 170
        and g >= r * 1.35
        and g >= b * 1.05
    )


def _is_dark_wechat_background(pixel: Any) -> bool:
    try:
        r, g, b = [int(value) for value in pixel[:3]]
    except Exception:
        return False
    return max(r, g, b) <= 55 and max(r, g, b) - min(r, g, b) <= 24


def _is_wechat_neutral_status_pixel(pixel: Any) -> bool:
    try:
        r, g, b = [int(value) for value in pixel[:3]]
    except Exception:
        return False
    return 70 <= r <= 230 and 70 <= g <= 230 and 70 <= b <= 230 and max(r, g, b) - min(r, g, b) <= 45


def _is_wechat_error_status_pixel(pixel: Any) -> bool:
    try:
        r, g, b = [int(value) for value in pixel[:3]]
    except Exception:
        return False
    return r >= 150 and g <= 95 and b <= 95 and r >= g * 1.6 and r >= b * 1.6


def pc_weixin_capture_dir() -> Path:
    raw_dir = os.environ.get("WECHAT_PC_BRIDGE_CAPTURE_DIR", "").strip()
    if raw_dir:
        return Path(raw_dir)
    return Path(__file__).resolve().parents[2] / "runtime" / "data" / "pc-weixin-captures"


def safe_capture_id(value: str) -> str:
    safe_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value or ""))[:80]
    return safe_id or str(time.time_ns())


def save_capture_image(image: Any, *, item_id: str, phase: str) -> str:
    if image is None or not item_id:
        return ""
    path = pc_weixin_capture_dir() / f"send-{safe_capture_id(item_id)}-{phase}.png"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        image.save(path)
        return str(path)
    except Exception as exc:
        return f"capture_failed: {exc}"


def outgoing_green_bubble_delta(before_image: Any, after_image: Any) -> dict[str, Any]:
    """Detect a newly visible right-side WeChat outgoing bubble in a window screenshot pair."""
    try:
        before = before_image.convert("RGB")
        after = after_image.convert("RGB")
    except Exception as exc:
        return {"verified": False, "error": f"image_convert_failed: {exc}"}
    if before.size != after.size:
        return {"verified": False, "error": "image_size_changed", "before_size": before.size, "after_size": after.size}

    width, height = after.size
    x_min = int(width * 0.35)
    x_max = int(width * 0.97)
    y_min = int(height * 0.08)
    y_max = int(height * 0.80)
    # Old outgoing bubbles can move when WeChat scrolls; accept only the lower
    # chat band where a freshly sent message should appear above the composer.
    accepted_y_min = max(int(height * 0.48), y_max - int(height * 0.30))
    points: set[tuple[int, int]] = set()
    before_px = before.load()
    after_px = after.load()
    for y in range(y_min, y_max):
        for x in range(x_min, x_max):
            after_green = _is_wechat_outgoing_green(after_px[x, y])
            if not after_green:
                continue
            if _is_wechat_outgoing_green(before_px[x, y]):
                continue
            points.add((x, y))

    components: list[dict[str, int]] = []
    while points:
        start = points.pop()
        stack = [start]
        min_x = max_x = start[0]
        min_y = max_y = start[1]
        count = 0
        while stack:
            x, y = stack.pop()
            count += 1
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)
            for neighbor in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if neighbor in points:
                    points.remove(neighbor)
                    stack.append(neighbor)
        comp_width = max_x - min_x + 1
        comp_height = max_y - min_y + 1
        center_x = (min_x + max_x) // 2
        components.append(
            {
                "pixels": count,
                "left": min_x,
                "top": min_y,
                "right": max_x,
                "bottom": max_y,
                "width": comp_width,
                "height": comp_height,
                "center_x": center_x,
            }
        )

    components.sort(key=lambda item: item["pixels"], reverse=True)
    valid = [
        item
        for item in components
        if item["pixels"] >= 120
        and item["width"] >= 24
        and item["height"] >= 14
        and item["center_x"] >= int(width * 0.55)
        and item["bottom"] >= accepted_y_min
    ]
    valid.sort(key=lambda item: (item["bottom"], item["pixels"]), reverse=True)
    return {
        "verified": bool(valid),
        "candidate_count": len(valid),
        "new_green_component_count": len(components),
        "largest_component": components[0] if components else {},
        "matched_component": valid[0] if valid else {},
        "region": {"left": x_min, "top": y_min, "right": x_max, "bottom": y_max},
        "accepted_band": {"top": accepted_y_min, "bottom": y_max},
        "rejected_top_green_components": len(
            [
                item
                for item in components
                if item["pixels"] >= 120
                and item["width"] >= 24
                and item["height"] >= 14
                and item["center_x"] >= int(width * 0.55)
                and item["bottom"] < accepted_y_min
            ]
        ),
    }


def outgoing_media_delivery_status(after_image: Any, media_component: dict[str, Any]) -> dict[str, Any]:
    """Detect WeChat's small pending/failed marker beside an outgoing media bubble."""
    if not media_component:
        return {"state": "missing_media_component", "stable": False, "pending": False, "failed": False}
    try:
        after = after_image.convert("RGB")
    except Exception as exc:
        return {"state": "image_convert_failed", "stable": False, "pending": False, "failed": False, "error": str(exc)}

    width, height = after.size
    try:
        media_left = int(media_component.get("left", 0))
        media_top = int(media_component.get("top", 0))
        media_bottom = int(media_component.get("bottom", 0))
    except Exception:
        return {"state": "invalid_media_component", "stable": False, "pending": False, "failed": False}
    if media_left <= 0 or media_bottom <= media_top:
        return {"state": "invalid_media_component", "stable": False, "pending": False, "failed": False}

    x_min = max(0, media_left - 56)
    x_max = max(0, media_left - 3)
    y_min = max(0, media_top - 8)
    y_max = min(height, media_bottom + 8)
    if x_max <= x_min or y_max <= y_min:
        return {"state": "no_status_region", "stable": True, "pending": False, "failed": False}

    after_px = after.load()
    pending_points: set[tuple[int, int]] = set()
    failure_points: set[tuple[int, int]] = set()
    for y in range(y_min, y_max):
        for x in range(x_min, x_max):
            pixel = after_px[x, y]
            if _is_wechat_error_status_pixel(pixel):
                failure_points.add((x, y))
            elif _is_wechat_neutral_status_pixel(pixel) and not _is_dark_wechat_background(pixel):
                pending_points.add((x, y))

    def components(points: set[tuple[int, int]]) -> list[dict[str, int]]:
        result: list[dict[str, int]] = []
        while points:
            start = points.pop()
            stack = [start]
            min_x = max_x = start[0]
            min_y = max_y = start[1]
            count = 0
            while stack:
                x, y = stack.pop()
                count += 1
                min_x = min(min_x, x)
                max_x = max(max_x, x)
                min_y = min(min_y, y)
                max_y = max(max_y, y)
                for neighbor in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if neighbor in points:
                        points.remove(neighbor)
                        stack.append(neighbor)
            result.append(
                {
                    "pixels": count,
                    "left": min_x,
                    "top": min_y,
                    "right": max_x,
                    "bottom": max_y,
                    "width": max_x - min_x + 1,
                    "height": max_y - min_y + 1,
                    "center_x": (min_x + max_x) // 2,
                    "center_y": (min_y + max_y) // 2,
                }
            )
        result.sort(key=lambda item: item["pixels"], reverse=True)
        return result

    pending_components = [
        item
        for item in components(pending_points)
        if 25 <= item["pixels"] <= 650
        and 8 <= item["width"] <= 34
        and 8 <= item["height"] <= 34
        and item["right"] < media_left
    ]
    failure_components = [
        item
        for item in components(failure_points)
        if 12 <= item["pixels"] <= 650
        and 6 <= item["width"] <= 36
        and 6 <= item["height"] <= 36
        and item["right"] < media_left
    ]
    failed = bool(failure_components)
    pending = bool(pending_components)
    state = "failed" if failed else ("pending" if pending else "stable")
    return {
        "state": state,
        "stable": state == "stable",
        "pending": pending,
        "failed": failed,
        "pending_indicator_count": len(pending_components),
        "failure_indicator_count": len(failure_components),
        "pending_indicator": pending_components[0] if pending_components else {},
        "failure_indicator": failure_components[0] if failure_components else {},
        "region": {"left": x_min, "top": y_min, "right": x_max, "bottom": y_max},
    }


def _stable_outgoing_media_components(after_image: Any) -> dict[str, Any]:
    """Find right-side media thumbnails in the final screenshot, even with weak pixel deltas."""
    try:
        after = after_image.convert("RGB")
    except Exception as exc:
        return {"components": [], "valid": [], "error": f"image_convert_failed: {exc}"}

    width, height = after.size
    x_min = int(width * 0.35)
    x_max = int(width * 0.97)
    y_min = int(height * 0.08)
    y_max = int(height * 0.80)
    accepted_y_min = max(int(height * 0.45), y_max - int(height * 0.34))
    after_px = after.load()
    points: set[tuple[int, int]] = set()
    for y in range(y_min, y_max):
        for x in range(x_min, x_max):
            pixel = after_px[x, y]
            if _is_dark_wechat_background(pixel):
                continue
            if _is_wechat_outgoing_green(pixel):
                continue
            points.add((x, y))

    components: list[dict[str, Any]] = []
    while points:
        start = points.pop()
        stack = [start]
        min_x = max_x = start[0]
        min_y = max_y = start[1]
        count = 0
        green_like = 0
        background_like = 0
        brightness_sum = 0
        color_bucket_count: dict[tuple[int, int, int], int] = {}
        while stack:
            x, y = stack.pop()
            count += 1
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)
            ar, ag, ab = [int(value) for value in after_px[x, y][:3]]
            if ag >= 120 and ag >= ar + 35 and ag >= ab + 25:
                green_like += 1
            if max(ar, ag, ab) <= 48 and max(ar, ag, ab) - min(ar, ag, ab) <= 18:
                background_like += 1
            brightness_sum += ar + ag + ab
            color_bucket = (ar // 32, ag // 32, ab // 32)
            color_bucket_count[color_bucket] = color_bucket_count.get(color_bucket, 0) + 1
            for neighbor in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if neighbor in points:
                    points.remove(neighbor)
                    stack.append(neighbor)

        comp_width = max_x - min_x + 1
        comp_height = max_y - min_y + 1
        center_x = (min_x + max_x) // 2
        center_y = (min_y + max_y) // 2
        green_ratio = green_like / max(1, count)
        background_ratio = background_like / max(1, count)
        fill_ratio = count / max(1, comp_width * comp_height)
        color_bucket_ratio = len(color_bucket_count) / max(1, count)
        components.append(
            {
                "pixels": count,
                "left": min_x,
                "top": min_y,
                "right": max_x,
                "bottom": max_y,
                "width": comp_width,
                "height": comp_height,
                "center_x": center_x,
                "center_y": center_y,
                "green_ratio": round(green_ratio, 3),
                "background_ratio": round(background_ratio, 3),
                "fill_ratio": round(fill_ratio, 3),
                "avg_brightness": round(brightness_sum / max(1, count * 3), 1),
                "color_bucket_count": len(color_bucket_count),
                "color_bucket_ratio": round(color_bucket_ratio, 3),
            }
        )

    components.sort(key=lambda item: item["pixels"], reverse=True)
    max_thumb_width = max(72, int(width * 0.42))
    max_thumb_height = max(72, int(height * 0.48))
    valid = [
        item
        for item in components
        if item["pixels"] >= 1000
        and item["width"] >= 48
        and item["height"] >= 48
        and item["width"] <= max_thumb_width
        and item["height"] <= max_thumb_height
        and item["center_x"] >= int(width * 0.58)
        and item["right"] >= int(width * 0.72)
        and item["bottom"] >= accepted_y_min
        and item.get("fill_ratio", 0.0) >= 0.08
        and item.get("green_ratio", 1.0) <= 0.55
        and item.get("background_ratio", 1.0) <= 0.35
    ]
    valid.sort(key=lambda item: (item["bottom"], item["pixels"]), reverse=True)
    return {
        "components": components,
        "valid": valid,
        "largest_component": components[0] if components else {},
        "matched_component": valid[0] if valid else {},
        "region": {"left": x_min, "top": y_min, "right": x_max, "bottom": y_max},
        "accepted_band": {"top": accepted_y_min, "bottom": y_max},
    }


def outgoing_media_delta(before_image: Any, after_image: Any) -> dict[str, Any]:
    """Detect a newly visible right-side outgoing media thumbnail in screenshots."""
    try:
        before = before_image.convert("RGB")
        after = after_image.convert("RGB")
    except Exception as exc:
        return {"verified": False, "error": f"image_convert_failed: {exc}"}
    if before.size != after.size:
        return {"verified": False, "error": "image_size_changed", "before_size": before.size, "after_size": after.size}

    width, height = after.size
    x_min = int(width * 0.35)
    x_max = int(width * 0.97)
    y_min = int(height * 0.08)
    y_max = int(height * 0.80)
    accepted_y_min = max(int(height * 0.45), y_max - int(height * 0.34))
    before_px = before.load()
    after_px = after.load()
    points: set[tuple[int, int]] = set()
    for y in range(y_min, y_max):
        for x in range(x_min, x_max):
            br, bg, bb = [int(value) for value in before_px[x, y][:3]]
            ar, ag, ab = [int(value) for value in after_px[x, y][:3]]
            diff = abs(ar - br) + abs(ag - bg) + abs(ab - bb)
            if diff >= 45:
                points.add((x, y))

    components: list[dict[str, Any]] = []
    while points:
        start = points.pop()
        stack = [start]
        min_x = max_x = start[0]
        min_y = max_y = start[1]
        count = 0
        green_like = 0
        background_like = 0
        brightness_sum = 0
        color_bucket_count: dict[tuple[int, int, int], int] = {}
        while stack:
            x, y = stack.pop()
            count += 1
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)
            ar, ag, ab = [int(value) for value in after_px[x, y][:3]]
            if ag >= 120 and ag >= ar + 35 and ag >= ab + 25:
                green_like += 1
            if max(ar, ag, ab) <= 48 and max(ar, ag, ab) - min(ar, ag, ab) <= 18:
                background_like += 1
            brightness_sum += ar + ag + ab
            color_bucket = (ar // 32, ag // 32, ab // 32)
            color_bucket_count[color_bucket] = color_bucket_count.get(color_bucket, 0) + 1
            for neighbor in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if neighbor in points:
                    points.remove(neighbor)
                    stack.append(neighbor)
        comp_width = max_x - min_x + 1
        comp_height = max_y - min_y + 1
        center_x = (min_x + max_x) // 2
        center_y = (min_y + max_y) // 2
        green_ratio = green_like / max(1, count)
        background_ratio = background_like / max(1, count)
        color_bucket_ratio = len(color_bucket_count) / max(1, count)
        components.append(
            {
                "pixels": count,
                "left": min_x,
                "top": min_y,
                "right": max_x,
                "bottom": max_y,
                "width": comp_width,
                "height": comp_height,
                "center_x": center_x,
                "center_y": center_y,
                "green_ratio": round(green_ratio, 3),
                "background_ratio": round(background_ratio, 3),
                "avg_brightness": round(brightness_sum / max(1, count * 3), 1),
                "color_bucket_count": len(color_bucket_count),
                "color_bucket_ratio": round(color_bucket_ratio, 3),
            }
        )

    components.sort(key=lambda item: item["pixels"], reverse=True)
    valid = [
        item
        for item in components
        if item["pixels"] >= 800
        and item["width"] >= 48
        and item["height"] >= 48
        and item["center_x"] >= int(width * 0.50)
        and item["bottom"] >= accepted_y_min
        and item.get("green_ratio", 1.0) <= 0.55
        and item.get("background_ratio", 1.0) <= 0.65
    ]
    valid.sort(key=lambda item: (item["bottom"], item["pixels"]), reverse=True)
    stable_scan: dict[str, Any] = _stable_outgoing_media_components(after)
    stable_valid = stable_scan.get("valid") if isinstance(stable_scan, dict) else []
    detection_method = "changed_delta"
    if isinstance(stable_valid, list) and stable_valid:
        changed_match = valid[0] if valid else {}
        changed_width = int(changed_match.get("width", 0) or 0) if isinstance(changed_match, dict) else 0
        changed_left = int(changed_match.get("left", 0) or 0) if isinstance(changed_match, dict) else 0
        stable_left = int(stable_valid[0].get("left", 0) or 0)
        prefer_stable_bounds = (
            not valid
            or changed_width > max(72, int(width * 0.30))
            or (stable_left and changed_left and changed_left < stable_left - 40)
        )
        if prefer_stable_bounds:
            valid = stable_valid
            detection_method = "stable_after_image"
    delivery_status = outgoing_media_delivery_status(after_image, valid[0]) if valid else {
        "state": "missing_media_component",
        "stable": False,
        "pending": False,
        "failed": False,
    }
    media_visible = bool(valid)
    verified = media_visible and bool(delivery_status.get("stable"))
    return {
        "verified": verified,
        "media_visible": media_visible,
        "candidate_count": len(valid),
        "changed_component_count": len(components),
        "stable_component_count": len(stable_scan.get("components", [])) if isinstance(stable_scan, dict) else 0,
        "stable_candidate_count": len(stable_scan.get("valid", [])) if isinstance(stable_scan, dict) else 0,
        "detection_method": detection_method,
        "largest_component": components[0] if components else {},
        "stable_largest_component": stable_scan.get("largest_component", {}) if isinstance(stable_scan, dict) else {},
        "matched_component": valid[0] if valid else {},
        "delivery_status": delivery_status,
        "region": {"left": x_min, "top": y_min, "right": x_max, "bottom": y_max},
        "accepted_band": {"top": accepted_y_min, "bottom": y_max},
        "rejected_top_components": len(
            [
                item
                for item in components
                if item["pixels"] >= 800
                and item["width"] >= 48
                and item["height"] >= 36
                and item["center_x"] >= int(width * 0.50)
                and item["bottom"] < accepted_y_min
            ]
        ),
        "rejected_green_text_components": len(
            [
                item
                for item in components
                if item["pixels"] >= 800
                and item["width"] >= 48
                and item["height"] >= 36
                and item["center_x"] >= int(width * 0.50)
                and item["bottom"] >= accepted_y_min
                and item.get("green_ratio", 0.0) > 0.55
            ]
        ),
        "rejected_background_components": len(
            [
                item
                for item in components
                if item["pixels"] >= 800
                and item["width"] >= 48
                and item["height"] >= 36
                and item["center_x"] >= int(width * 0.50)
                and item["bottom"] >= accepted_y_min
                and item.get("background_ratio", 0.0) > 0.65
            ]
        ),
    }


def media_preview_delta(before_image: Any, after_image: Any) -> dict[str, Any]:
    """Detect a media preview inserted into the lower compose area before submit."""
    try:
        before = before_image.convert("RGB")
        after = after_image.convert("RGB")
    except Exception as exc:
        return {"verified": False, "error": f"image_convert_failed: {exc}"}
    if before.size != after.size:
        return {"verified": False, "error": "image_size_changed", "before_size": before.size, "after_size": after.size}

    width, height = after.size
    x_min = int(width * 0.25)
    x_max = int(width * 0.98)
    y_min = int(height * 0.70)
    y_max = int(height * 0.98)
    before_px = before.load()
    after_px = after.load()
    points: set[tuple[int, int]] = set()
    for y in range(y_min, y_max):
        for x in range(x_min, x_max):
            br, bg, bb = [int(value) for value in before_px[x, y][:3]]
            ar, ag, ab = [int(value) for value in after_px[x, y][:3]]
            diff = abs(ar - br) + abs(ag - bg) + abs(ab - bb)
            if diff >= 45:
                points.add((x, y))

    components: list[dict[str, Any]] = []
    while points:
        start = points.pop()
        stack = [start]
        min_x = max_x = start[0]
        min_y = max_y = start[1]
        count = 0
        green_like = 0
        background_like = 0
        brightness_sum = 0
        color_bucket_count: dict[tuple[int, int, int], int] = {}
        while stack:
            x, y = stack.pop()
            count += 1
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)
            ar, ag, ab = [int(value) for value in after_px[x, y][:3]]
            if ag >= 120 and ag >= ar + 35 and ag >= ab + 25:
                green_like += 1
            if max(ar, ag, ab) <= 48 and max(ar, ag, ab) - min(ar, ag, ab) <= 18:
                background_like += 1
            brightness_sum += ar + ag + ab
            color_bucket = (ar // 32, ag // 32, ab // 32)
            color_bucket_count[color_bucket] = color_bucket_count.get(color_bucket, 0) + 1
            for neighbor in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if neighbor in points:
                    points.remove(neighbor)
                    stack.append(neighbor)
        comp_width = max_x - min_x + 1
        comp_height = max_y - min_y + 1
        center_x = (min_x + max_x) // 2
        center_y = (min_y + max_y) // 2
        green_ratio = green_like / max(1, count)
        background_ratio = background_like / max(1, count)
        color_bucket_ratio = len(color_bucket_count) / max(1, count)
        components.append(
            {
                "pixels": count,
                "left": min_x,
                "top": min_y,
                "right": max_x,
                "bottom": max_y,
                "width": comp_width,
                "height": comp_height,
                "center_x": center_x,
                "center_y": center_y,
                "green_ratio": round(green_ratio, 3),
                "background_ratio": round(background_ratio, 3),
                "avg_brightness": round(brightness_sum / max(1, count * 3), 1),
                "color_bucket_count": len(color_bucket_count),
                "color_bucket_ratio": round(color_bucket_ratio, 3),
            }
        )

    components.sort(key=lambda item: item["pixels"], reverse=True)
    valid = [
        item
        for item in components
        if item["pixels"] >= 800
        and item["width"] >= 48
        and item["height"] >= 48
        and item.get("green_ratio", 1.0) <= 0.55
        and item.get("background_ratio", 1.0) <= 0.70
    ]
    valid.sort(key=lambda item: (item["pixels"], item["height"], item["width"]), reverse=True)
    return {
        "verified": bool(valid),
        "candidate_count": len(valid),
        "changed_component_count": len(components),
        "largest_component": components[0] if components else {},
        "matched_component": valid[0] if valid else {},
        "region": {"left": x_min, "top": y_min, "right": x_max, "bottom": y_max},
        "rejected_green_components": len(
            [
                item
                for item in components
                if item["pixels"] >= 800
                and item["width"] >= 48
                and item["height"] >= 36
                and item.get("green_ratio", 0.0) > 0.55
            ]
        ),
        "rejected_background_components": len(
            [
                item
                for item in components
                if item["pixels"] >= 800
                and item["width"] >= 48
                and item["height"] >= 36
                and item.get("background_ratio", 0.0) > 0.70
            ]
        ),
    }


@dataclass
class QueueItem:
    id: str
    action: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)


class AdapterApi:
    def __init__(self, base_url: str, connector_id: str, secret: str = ""):
        self.base_url = base_url.rstrip("/")
        self.connector_id = connector_id
        self.secret = secret

    def heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = f"/api/connectors/{urllib.parse.quote(self.connector_id)}/heartbeat"
        return self._request("POST", path, payload)

    def poll(self, limit: int) -> list[QueueItem]:
        path = f"/api/connectors/{urllib.parse.quote(self.connector_id)}/poll?limit={int(limit)}"
        data = self._request("GET", path)
        return [
            QueueItem(
                id=str(item.get("id") or ""),
                action=dict(item.get("action") or {}),
                metadata=dict(item.get("metadata") or {}) if isinstance(item.get("metadata"), dict) else {},
            )
            for item in data.get("items", [])
            if item.get("id")
        ]

    def ack(self, item_id: str) -> dict[str, Any]:
        path = f"/api/connectors/{urllib.parse.quote(self.connector_id)}/ack"
        return self._request("POST", path, {"id": item_id})

    def delivery(
        self,
        item_id: str,
        *,
        status: str,
        delivered: bool,
        dry_run: bool,
        ack: bool,
        metadata: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        path = f"/api/connectors/{urllib.parse.quote(self.connector_id)}/delivery"
        return self._request(
            "POST",
            path,
            {
                "id": item_id,
                "status": status,
                "ok": delivered,
                "delivered": delivered,
                "dry_run": dry_run,
                "ack": ack,
                "metadata": metadata or {},
                "error": error,
            },
        )

    def post_inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/api/inbound", payload)

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self.secret:
            headers["X-WeChat-Agent-Secret"] = self.secret
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise BridgeError(f"adapter api {method} {path} failed: {exc.code} {raw}") from exc
        except urllib.error.URLError as exc:
            raise BridgeError(f"adapter api is unreachable: {exc}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BridgeError(f"adapter api returned non-json: {raw[:200]}") from exc


class Win32WeixinController:
    def __init__(
        self,
        *,
        search_mode: str = "enter",
        submit_method: str = "button",
        settle_seconds: float = 0.7,
    ):
        self.search_mode = search_mode
        self.submit_method = submit_method
        self.settle_seconds = settle_seconds
        self._load_win32()
        self.hwnd = self.find_window()

    def _load_win32(self) -> None:
        try:
            import psutil  # type: ignore
            import pyperclip  # type: ignore
            import win32api  # type: ignore
            import win32con  # type: ignore
            import win32gui  # type: ignore
            import win32process  # type: ignore
        except ImportError as exc:
            raise BridgeError(
                "pc_weixin bridge needs psutil, pyperclip and pywin32. "
                "Install them in the test venv before real UI automation."
            ) from exc
        self.psutil = psutil
        self.pyperclip = pyperclip
        self.win32api = win32api
        self.win32con = win32con
        self.win32gui = win32gui
        self.win32process = win32process

    def find_window(self) -> int:
        matches: list[tuple[int, str, str, str, int, int]] = []

        def callback(hwnd: int, _: object) -> None:
            if not self.win32gui.IsWindowVisible(hwnd):
                return
            title = self.win32gui.GetWindowText(hwnd)
            class_name = self.win32gui.GetClassName(hwnd)
            _, pid = self.win32process.GetWindowThreadProcessId(hwnd)
            try:
                proc = self.psutil.Process(pid)
                name = proc.name()
                path = proc.exe()
            except Exception:
                name = ""
                path = ""
            is_weixin = name.lower() == "weixin.exe" or path.lower().endswith("\\weixin.exe")
            is_main = title == "微信" or class_name.startswith("Qt")
            if is_weixin and is_main:
                left, top, right, bottom = self.win32gui.GetWindowRect(hwnd)
                matches.append((hwnd, title, class_name, path, max(0, right - left), max(0, bottom - top)))

        self.win32gui.EnumWindows(callback, None)
        if not matches:
            raise BridgeError("Weixin main window was not found. Open PC WeChat first.")
        # Prefer a real chat window when a relogin/security prompt is still visible.
        matches.sort(
            key=lambda item: (
                is_probable_login_required_window(
                    title=item[1],
                    class_name=item[2],
                    width=item[4],
                    height=item[5],
                ),
                item[1] != "微信",
                -(item[4] * item[5]),
                item[0],
            )
        )
        return matches[0][0]

    def probe(self) -> dict[str, Any]:
        left, top, right, bottom = self.win32gui.GetWindowRect(self.hwnd)
        _, pid = self.win32process.GetWindowThreadProcessId(self.hwnd)
        proc = self.psutil.Process(pid)
        width = max(0, right - left)
        height = max(0, bottom - top)
        class_name = self.win32gui.GetClassName(self.hwnd)
        title = self.win32gui.GetWindowText(self.hwnd)
        login_required = is_probable_login_required_window(
            title=title,
            class_name=class_name,
            width=width,
            height=height,
        )
        return {
            "hwnd": self.hwnd,
            "title": title,
            "class": class_name,
            "login_required": login_required,
            "pid": pid,
            "process": proc.name(),
            "path": proc.exe(),
            "rect": [left, top, right, bottom],
            "size": [width, height],
        }

    def send_text(self, chat: str, text: str, *, evidence_id: str = "") -> dict[str, Any]:
        self.activate()
        self.raise_if_login_required()
        if chat and self.search_mode != "none":
            self.search_chat(chat)
        self.click_fraction(COMPOSE_X, COMPOSE_Y)
        self.hotkey("ctrl", "a")
        self.paste_text(text)
        time.sleep(0.2)
        compose_before = self.copy_compose_text()
        if not compose_still_contains_submitted_text(text, compose_before):
            raise BridgeError("compose verification failed before send; pasted text was not found")

        self.click_fraction(COMPOSE_X, COMPOSE_Y)
        if self.submit_method == "enter":
            self.key("enter")
            submit_used = "enter"
        else:
            submit_used = self.click_send_button_fallback()
        time.sleep(self.settle_seconds)
        compose_after = self.copy_compose_text()
        if compose_still_contains_submitted_text(text, compose_after):
            raise BridgeError("message still in compose box after send; not acking")
        return {
            "verified": True,
            "chat": chat,
            "submit_method": submit_used,
            "text_chars": len(text),
            "compose_before_chars": len(compose_before),
            "compose_after_chars": len(compose_after),
        }

    def send_image(
        self,
        chat: str,
        image_path: str,
        *,
        preserve_animation: bool = False,
        evidence_id: str = "",
    ) -> dict[str, Any]:
        path = Path(image_path)
        if not path.is_file():
            raise BridgeError(f"media file not found: {path}")

        self.activate()
        self.raise_if_login_required()
        if chat and self.search_mode != "none":
            self.search_chat(chat)
        self.click_fraction(COMPOSE_X, COMPOSE_Y)
        self.hotkey("ctrl", "a")
        self.key("delete")

        clipboard_method = copy_media_to_clipboard(path, preserve_animation=preserve_animation)
        time.sleep(0.15)
        self.hotkey("ctrl", "v")
        time.sleep(0.5)

        if self.submit_method == "enter":
            self.key("enter")
            submit_used = "enter"
        else:
            submit_used = self.click_send_button_fallback()
        time.sleep(self.settle_seconds)
        return {
            "verified": "win32_paste_submit",
            "chat": chat,
            "submit_method": submit_used,
            "path": str(path),
            "preserve_animation": bool(preserve_animation),
            "clipboard_method": clipboard_method,
        }

    def search_chat(self, chat: str) -> None:
        self.click_fraction(SEARCH_X, SEARCH_Y)
        self.hotkey("ctrl", "a")
        self.paste_text(chat)
        time.sleep(self.settle_seconds)
        if self.search_mode == "click":
            self.click_fraction(SEARCH_RESULT_X, SEARCH_RESULT_Y)
        elif self.search_mode == "none":
            return
        else:
            self.key("enter")
        time.sleep(self.settle_seconds)

    def copy_compose_text(self) -> str:
        sentinel = f"__wechat_agent_empty_clipboard_{time.time_ns()}__"
        try:
            previous_clipboard = str(self.pyperclip.paste() or "")
        except Exception:
            previous_clipboard = ""
        try:
            self.pyperclip.copy(sentinel)
            self.click_fraction(COMPOSE_X, COMPOSE_Y)
            self.hotkey("ctrl", "a")
            self.hotkey("ctrl", "c")
            time.sleep(0.08)
            copied = str(self.pyperclip.paste() or "")
            return "" if copied == sentinel else copied
        finally:
            try:
                self.pyperclip.copy(previous_clipboard)
            except Exception:
                pass

    def capture(self, path: Path) -> Path:
        try:
            from PIL import ImageGrab  # type: ignore
        except ImportError as exc:
            raise BridgeError("capture needs Pillow installed") from exc
        self.activate()
        bbox = self.win32gui.GetWindowRect(self.hwnd)
        path.parent.mkdir(parents=True, exist_ok=True)
        image = ImageGrab.grab(bbox=bbox)
        image.save(path)
        return path

    def activate(self) -> None:
        self.win32gui.ShowWindow(self.hwnd, self.win32con.SW_RESTORE)
        try:
            self.win32gui.SetForegroundWindow(self.hwnd)
        except Exception:
            # Some foreground restrictions are transient; the following click/keys
            # still work when Weixin is already visible.
            pass
        time.sleep(0.2)

    def click_fraction(self, x_fraction: float, y_fraction: float) -> None:
        left, top, right, bottom = self.win32gui.GetWindowRect(self.hwnd)
        x = left + int((right - left) * x_fraction)
        y = top + int((bottom - top) * y_fraction)
        self.win32api.SetCursorPos((x, y))
        self.win32api.mouse_event(self.win32con.MOUSEEVENTF_LEFTDOWN, x, y, 0, 0)
        self.win32api.mouse_event(self.win32con.MOUSEEVENTF_LEFTUP, x, y, 0, 0)
        time.sleep(0.1)

    def click_send_button_fallback(self) -> str:
        used = []
        for x_fraction, y_fraction in SEND_BUTTON_FALLBACK_POINTS:
            self.click_fraction(x_fraction, y_fraction)
            used.append(f"{x_fraction:.3f},{y_fraction:.3f}")
            time.sleep(0.1)
        return "button_coordinate_fallback:" + ";".join(used)

    def raise_if_login_required(self) -> None:
        class_name = self.win32gui.GetClassName(self.hwnd)
        left, top, right, bottom = self.win32gui.GetWindowRect(self.hwnd)
        if is_probable_login_required_window(
            title=self.win32gui.GetWindowText(self.hwnd),
            class_name=class_name,
            width=max(0, right - left),
            height=max(0, bottom - top),
        ):
            raise BridgeError(login_required_error(class_name))

    def paste_text(self, text: str) -> None:
        self.pyperclip.copy(text)
        time.sleep(0.08)
        self.hotkey("ctrl", "v")

    def hotkey(self, *keys: str) -> None:
        codes = [self._vk(key) for key in keys]
        for code in codes:
            self.win32api.keybd_event(code, 0, 0, 0)
        for code in reversed(codes):
            self.win32api.keybd_event(code, 0, self.win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.08)

    def key(self, key: str) -> None:
        code = self._vk(key)
        self.win32api.keybd_event(code, 0, 0, 0)
        self.win32api.keybd_event(code, 0, self.win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.08)

    def _vk(self, key: str) -> int:
        lookup = {
            "ctrl": self.win32con.VK_CONTROL,
            "control": self.win32con.VK_CONTROL,
            "shift": self.win32con.VK_SHIFT,
            "alt": self.win32con.VK_MENU,
            "enter": self.win32con.VK_RETURN,
            "return": self.win32con.VK_RETURN,
            "delete": self.win32con.VK_DELETE,
            "del": self.win32con.VK_DELETE,
            "backspace": self.win32con.VK_BACK,
            "esc": self.win32con.VK_ESCAPE,
        }
        value = lookup.get(key.lower())
        if value is not None:
            return value
        if len(key) == 1:
            return ord(key.upper())
        raise BridgeError(f"unsupported key: {key}")


class UiaWeixinController:
    WECHAT_TITLES = ("微信", "WeChat")
    EXCLUDE_CLASSES = {"Chrome_WidgetWin_1", "CabinetWClass", "CASCADIA_HOSTING_WINDOW_CLASS"}
    NATIVE_WINDOW_CLASSES = ("Qt51514QWindowIcon", "WeChatMainWndForPC")
    NATIVE_FOREGROUND_ATTEMPTS = 4
    _VISUAL_OCR_READER: Any = None
    _VISUAL_OCR_ERROR = ""
    GA_ROOT = 2
    HWND_NOTOPMOST = -2
    HWND_TOPMOST = -1
    SW_RESTORE = 9
    SW_SHOW = 5
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_SHOWWINDOW = 0x0040

    def __init__(
        self,
        *,
        search_enabled: bool = True,
        submit_method: str = "button",
        settle_seconds: float = 0.7,
        assume_target_confirmed: bool = False,
    ):
        self.search_enabled = search_enabled
        self.submit_method = submit_method
        self.settle_seconds = settle_seconds
        # 仅手动 send-test 监督路径用：操作者已人工确认当前聊天即目标，跳过视觉核实。
        # 无人值守队列(run_poll/watch)默认 False，仍强制核实，安全不变。
        self._assume_target_confirmed = bool(assume_target_confirmed)
        self._lock = threading.Lock()
        self._load_uia()
        self._window = None
        self._input_control = None
        self._send_button = None
        self._last_contact = ""
        self._last_contact_switch_evidence: dict[str, Any] = {}
        self._last_target_evidence: dict[str, Any] = {}
        self._use_coord_fallback = False
        self._is_electron = False
        self._last_window_scan: dict[str, Any] = {}
        self.visible_conversation_switch_enabled = True
        self.visual_target_enabled = os.environ.get("WECHAT_PC_TARGET_VISUAL_OCR", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self._find_window()
        if self._window is None:
            scan = self._last_window_scan if isinstance(self._last_window_scan, dict) else {}
            if int(scan.get("root_child_count") or 0) == 0:
                raise BridgeError(
                    "Weixin main window was not found by UIA because no desktop windows were visible "
                    "to this process. Start the PC Weixin bridge from the interactive user desktop "
                    "or with elevated desktop access."
                )
            raise BridgeError("Weixin main window was not found by UIA. Open PC WeChat first.")

    def _load_uia(self) -> None:
        try:
            import pyperclip  # type: ignore
            import uiautomation as auto  # type: ignore
        except ImportError as exc:
            raise BridgeError(
                "uia automation needs uiautomation and pyperclip. "
                "Install them in the test venv before real UI automation."
            ) from exc
        self.auto = auto
        self.pyperclip = pyperclip

    def _window_size(self, window: Any) -> tuple[int, int]:
        try:
            rect = window.BoundingRectangle
            return max(0, int(rect.width())), max(0, int(rect.height()))
        except Exception:
            return (0, 0)

    def _native_window_rect_payload(self) -> dict[str, Any]:
        hwnd = self._find_native_hwnd()
        if not hwnd:
            return {}
        try:
            from ctypes import wintypes

            rect = wintypes.RECT()
            ok = bool(ctypes.windll.user32.GetWindowRect(int(hwnd), ctypes.byref(rect)))
        except Exception:
            return {}
        if not ok:
            return {}
        left = int(rect.left)
        top = int(rect.top)
        right = int(rect.right)
        bottom = int(rect.bottom)
        width = max(0, right - left)
        height = max(0, bottom - top)
        if not width or not height:
            return {}
        if left <= -30000 or top <= -30000:
            return {}
        return {
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
            "width": width,
            "height": height,
            "source": "native_hwnd",
            "native_hwnd": int(hwnd),
        }

    def _window_rect_payload(self) -> dict[str, Any]:
        try:
            payload: dict[str, Any] = rect_payload(getattr(self._window, "BoundingRectangle", None))
        except Exception:
            payload = {}
        width = int(payload.get("width") or 0)
        height = int(payload.get("height") or 0)
        if width > 0 and height > 0:
            payload["source"] = "uia"
            try:
                payload["native_hwnd"] = int(getattr(self._window, "NativeWindowHandle", 0) or 0)
            except Exception:
                payload["native_hwnd"] = 0
            return payload
        native_payload = self._native_window_rect_payload()
        if native_payload:
            if payload:
                native_payload["uia_rect"] = payload
            return native_payload
        if payload:
            payload["source"] = "uia_empty"
            return payload
        return {
            "left": 0,
            "top": 0,
            "right": 0,
            "bottom": 0,
            "width": 0,
            "height": 0,
            "source": "unavailable",
            "native_hwnd": 0,
        }

    def _window_login_required(self, window: Any) -> bool:
        try:
            name = str(window.Name or "")
            class_name = str(window.ClassName or "")
        except Exception:
            name = ""
            class_name = ""
        width, height = self._window_size(window)
        return is_probable_login_required_window(
            title=name,
            class_name=class_name,
            width=width,
            height=height,
        )

    def _window_process_identity(self, window: Any) -> dict[str, Any]:
        try:
            hwnd = int(getattr(window, "NativeWindowHandle", 0) or 0)
        except Exception:
            hwnd = 0
        return native_window_process_identity(hwnd)

    def _window_process_matches_wechat(self, window: Any) -> bool:
        return bool(self._window_process_identity(window).get("matches_wechat"))

    def _window_scan_entry(
        self,
        window: Any,
        *,
        process_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            name = str(window.Name or "")
        except Exception:
            name = ""
        try:
            class_name = str(window.ClassName or "")
        except Exception:
            class_name = ""
        try:
            handle = int(getattr(window, "NativeWindowHandle", 0) or 0)
        except Exception:
            handle = 0
        rect = rect_payload(getattr(window, "BoundingRectangle", None))
        width = int(rect.get("width") or 0)
        height = int(rect.get("height") or 0)
        if process_identity is None:
            process_identity = self._window_process_identity(window)
        title_exact = name in self.WECHAT_TITLES
        title_contains = any(title in name for title in self.WECHAT_TITLES)
        class_matches = class_name in self.NATIVE_WINDOW_CLASSES
        return {
            "name": name,
            "class": class_name,
            "handle": handle,
            "rect": rect,
            "area": width * height,
            "login_required": is_probable_login_required_window(
                title=name,
                class_name=class_name,
                width=width,
                height=height,
            ),
            "title_exact_wechat": title_exact,
            "title_contains_wechat": title_contains,
            "class_matches_wechat": class_matches,
            "process": process_identity or {},
        }

    def _find_window(self) -> None:
        root = self.auto.GetRootControl()
        children = list(root.GetChildren())
        candidates: list[tuple[Any, dict[str, Any]]] = []
        skipped: list[dict[str, Any]] = []
        for window in children:
            try:
                name = str(window.Name or "")
                class_name = str(window.ClassName or "")
            except Exception:
                continue
            if class_name in self.EXCLUDE_CLASSES:
                continue
            title_exact = name in self.WECHAT_TITLES
            title_contains = any(title in name for title in self.WECHAT_TITLES)
            class_matches = class_name in self.NATIVE_WINDOW_CLASSES
            process_identity = self._window_process_identity(window)
            process_matches = bool(process_identity.get("matches_wechat"))
            if title_exact or class_matches or process_matches:
                candidates.append((window, process_identity))
            elif len(skipped) < 12:
                entry = self._window_scan_entry(window, process_identity=process_identity)
                entry["title_contains_wechat"] = title_contains
                skipped.append(entry)
        self._last_window_scan = {
            "root_child_count": len(children),
            "candidate_count": len(candidates),
            "candidates": [
                self._window_scan_entry(window, process_identity=process_identity)
                for window, process_identity in candidates[:8]
            ],
            "skipped": skipped,
        }
        if not candidates:
            return

        def score(item: tuple[Any, dict[str, Any]]) -> tuple[bool, int, bool, int]:
            window = item[0]
            try:
                name = str(window.Name or "")
            except Exception:
                name = ""
            width, height = self._window_size(window)
            return (
                self._window_login_required(window),
                -(width * height),
                name not in self.WECHAT_TITLES,
                int(getattr(window, "NativeWindowHandle", 0) or 0),
            )

        candidates.sort(key=score)
        self._window = candidates[0][0]
        selected = self._window_scan_entry(self._window, process_identity=candidates[0][1])
        self._last_window_scan["selected"] = selected
        try:
            class_name = str(self._window.ClassName or "")
        except Exception:
            class_name = ""
        self._is_electron = class_name not in {"WeChatMainWndForPC"}

    def _ensure_window(self) -> bool:
        try:
            if self._window is not None and self._window.Exists(0.2):
                if self._window_login_required(self._window):
                    old_window = self._window
                    self._window = None
                    self._find_window()
                    if self._window is None:
                        self._window = old_window
                return True
        except Exception:
            pass
        self._window = None
        self._find_window()
        return self._window is not None

    def _window_class_name(self) -> str:
        try:
            return str(self._window.ClassName or "") if self._window is not None else ""
        except Exception:
            return ""

    def _login_required(self) -> bool:
        return self._window is not None and self._window_login_required(self._window)

    def _raise_if_login_required(self) -> None:
        class_name = self._window_class_name()
        if self._window is not None and self._window_login_required(self._window):
            raise BridgeError(login_required_error(class_name))

    def _target_chat_evidence(self, expected: str, *, allow_recent_switch: bool = True) -> dict[str, Any]:
        expected = str(expected or "").strip()
        evidence: dict[str, Any] = {
            "expected": expected,
            "status": "unavailable",
            "matched": False,
            "mismatch": False,
            "observed": "",
            "observed_names": [],
            "candidates": [],
        }
        if not expected:
            evidence["status"] = "missing_expected"
            return evidence
        try:
            if getattr(self, "_window", None) is None or not self._ensure_window():
                evidence["error"] = "window_unavailable"
                return evidence
            win_rect = self._window_rect_payload()
            win_width = max(0, int(win_rect.get("width") or 0))
            win_height = max(0, int(win_rect.get("height") or 0))
        except Exception as exc:
            evidence["error"] = f"window_probe_failed: {exc}"
            return evidence
        evidence["window_rect"] = dict(win_rect)
        if not win_width or not win_height:
            evidence["error"] = "window_rect_unavailable"
            return evidence

        win_left_value = int(win_rect.get("left") or 0)
        win_top_value = int(win_rect.get("top") or 0)
        win_bottom_value = int(win_rect.get("bottom") or 0)
        pane_left = win_left_value + max(220, int(win_width * 0.25))
        header_bottom = win_top_value + max(96, int(win_height * 0.18))
        candidates: list[dict[str, Any]] = []
        selected_conversation_candidates: list[dict[str, Any]] = []
        visible_conversation_candidates: list[dict[str, Any]] = []

        def in_header_region(rect: Any) -> bool:
            try:
                top = int(rect.top)
                left = int(rect.left)
                right = int(rect.right)
                width = max(0, int(rect.width()))
                height = max(0, int(rect.height()))
            except Exception:
                return False
            if not width or not height:
                return False
            center_x = left + width // 2
            return (
                top >= win_top_value
                and top <= header_bottom
                and center_x >= pane_left
                and height <= 90
                and right > pane_left
            )

        def in_conversation_list_region(rect: Any) -> bool:
            try:
                top = int(rect.top)
                left = int(rect.left)
                right = int(rect.right)
                width = max(0, int(rect.width()))
                height = max(0, int(rect.height()))
            except Exception:
                return False
            if not width or not height:
                return False
            center_x = left + width // 2
            return (
                center_x >= win_left_value + 40
                and center_x <= pane_left + 30
                and right <= pane_left + 80
                and top >= win_top_value + 48
                and top <= win_bottom_value - 80
                and height <= 96
            )

        def walk(control: Any, depth: int = 0, ancestor_selected: bool = False) -> None:
            if depth > 12 or len(candidates) >= 24:
                return
            try:
                children = control.GetChildren()
            except Exception:
                return
            for child in children:
                try:
                    name = str(getattr(child, "Name", "") or "").strip()
                    rect = getattr(child, "BoundingRectangle", None)
                    control_type = str(getattr(child, "ControlTypeName", "") or "")
                    selected_here = bool(ancestor_selected or self._control_looks_selected(child))
                    if name and len(name) <= 96 and rect is not None and in_header_region(rect):
                        candidate = control_payload(child)
                        candidate["matched"] = chat_label_matches(expected, name)
                        candidate["generic"] = is_generic_chat_target_label(name)
                        candidate["blocked_target"] = is_blocked_wechat_target_label(name)
                        candidate["strong_title_candidate"] = control_type in {
                            "TextControl",
                            "DocumentControl",
                        } and not bool(candidate["generic"])
                        candidates.append(candidate)
                    if (
                        name
                        and len(name) <= 96
                        and rect is not None
                        and in_conversation_list_region(rect)
                        and chat_label_matches(expected, name)
                    ):
                        visible = control_payload(child)
                        visible["matched"] = True
                        visible["selected"] = bool(selected_here)
                        visible_conversation_candidates.append(visible)
                        if selected_here:
                            selected = dict(visible)
                            selected["selected"] = True
                            selected_conversation_candidates.append(selected)
                    walk(child, depth + 1, selected_here)
                except Exception:
                    continue

        try:
            walk(self._window)
        except Exception as exc:
            evidence["error"] = f"walk_failed: {exc}"
            return evidence

        evidence["candidates"] = candidates[:12]
        evidence["observed_names"] = [str(candidate.get("name") or "") for candidate in candidates[:8]]
        if selected_conversation_candidates:
            evidence["selected_conversation_candidates"] = selected_conversation_candidates[:4]
        if visible_conversation_candidates:
            evidence["visible_conversation_candidates"] = visible_conversation_candidates[:6]
        matched = [candidate for candidate in candidates if candidate.get("matched") is True]
        if matched:
            evidence["status"] = "matched"
            evidence["matched"] = True
            evidence["observed"] = str(matched[0].get("name") or "")
            return evidence

        blocked_candidates = [candidate for candidate in candidates if candidate.get("blocked_target") is True]
        if blocked_candidates:
            evidence["status"] = "blocked_wechat_target_label"
            evidence["mismatch"] = True
            evidence["observed"] = str(blocked_candidates[0].get("name") or "")
            return evidence

        strong_candidates = [candidate for candidate in candidates if candidate.get("strong_title_candidate") is True]
        if strong_candidates:
            evidence["status"] = "mismatch"
            evidence["mismatch"] = True
            evidence["observed"] = str(strong_candidates[0].get("name") or "")
            return evidence

        if selected_conversation_candidates:
            evidence["status"] = "matched_by_selected_conversation"
            evidence["matched"] = True
            evidence["observed"] = str(selected_conversation_candidates[0].get("name") or "")
            return evidence

        recent_switch = self._recent_contact_switch_evidence(expected, require_verified=False)
        if recent_switch:
            evidence["recent_switch"] = recent_switch
        if allow_recent_switch and recent_switch and recent_switch.get("verified") is True:
            evidence["status"] = "matched_by_recent_switch"
            evidence["matched"] = True
            evidence["observed"] = str(recent_switch.get("contact") or "")
            return evidence

        if getattr(self, "visual_target_enabled", False):
            visual_evidence = self._visual_target_chat_evidence(expected)
            evidence["visual_evidence"] = visual_evidence
            if visual_evidence.get("matched") is True:
                evidence["status"] = str(visual_evidence.get("status") or "matched_by_visual_ocr")
                evidence["matched"] = True
                evidence["observed"] = str(visual_evidence.get("observed") or "")
                visual_observed = [
                    str(name or "")
                    for name in visual_evidence.get("observed_names") or []
                    if str(name or "")
                ]
                if visual_observed:
                    evidence["observed_names"] = [*evidence["observed_names"], *visual_observed]
                return evidence

        if candidates:
            evidence["status"] = "no_strong_title_candidate"
        else:
            evidence["status"] = "no_header_candidates"
        if expected:
            evidence["blocker_message"] = (
                f"当前聊天框未核实，已阻止发送；请切到目标 {expected} 或启用/修复视觉OCR定位。"
            )
        return evidence

    def _control_looks_selected(self, control: Any) -> bool:
        for attr in ("IsSelected", "HasKeyboardFocus"):
            try:
                if bool(getattr(control, attr, False)):
                    return True
            except Exception:
                pass
        for method_name in ("GetSelectionItemPattern", "GetLegacyIAccessiblePattern"):
            try:
                pattern = getattr(control, method_name)()
            except Exception:
                pattern = None
            if pattern is None:
                continue
            for attr in ("IsSelected", "CurrentIsSelected"):
                try:
                    if bool(getattr(pattern, attr, False)):
                        return True
                except Exception:
                    pass
        return False

    def _recent_contact_switch_evidence(self, expected: str, *, require_verified: bool = True) -> dict[str, Any]:
        raw = getattr(self, "_last_contact_switch_evidence", {})
        if not isinstance(raw, dict) or not raw.get("success"):
            return {}
        contact = str(raw.get("contact") or "")
        if not chat_label_matches(expected, contact):
            return {}
        try:
            switched_at = float(raw.get("completed_at") or raw.get("at") or 0.0)
        except Exception:
            switched_at = 0.0
        if not switched_at:
            return {}
        age_seconds = max(0.0, time.time() - switched_at)
        if age_seconds > RECENT_CONTACT_SWITCH_TTL_SECONDS:
            return {}
        verified = bool(raw.get("verified"))
        if require_verified and not verified:
            return {}
        return {
            "contact": contact,
            "method": str(raw.get("method") or ""),
            "age_seconds": round(age_seconds, 3),
            "success": True,
            "verified": verified,
            "source": "contact_search_action",
        }

    def _visible_conversation_controls(self, expected: str) -> list[Any]:
        expected = str(expected or "").strip()
        if not expected:
            return []
        if getattr(self, "_window", None) is None or not self._ensure_window():
            return []
        win_rect = self._window_rect_payload()
        win_width = max(0, int(win_rect.get("width") or 0))
        win_height = max(0, int(win_rect.get("height") or 0))
        if not win_width or not win_height:
            return []

        win_left_value = int(win_rect.get("left") or 0)
        win_top_value = int(win_rect.get("top") or 0)
        win_bottom_value = int(win_rect.get("bottom") or 0)
        pane_left = win_left_value + max(220, int(win_width * 0.25))

        def in_conversation_list_region(rect: Any) -> bool:
            try:
                top = int(rect.top)
                left = int(rect.left)
                right = int(rect.right)
                width = max(0, int(rect.width()))
                height = max(0, int(rect.height()))
            except Exception:
                return False
            if not width or not height:
                return False
            center_x = left + width // 2
            return (
                center_x >= win_left_value + 40
                and center_x <= pane_left + 30
                and right <= pane_left + 80
                and top >= win_top_value + 48
                and top <= win_bottom_value - 80
                and height <= 112
            )

        def best_click_target(child: Any, ancestors: list[Any]) -> Any:
            child_rect = getattr(child, "BoundingRectangle", None)
            try:
                child_height = max(0, int(child_rect.height()))
            except Exception:
                child_height = 0
            for ancestor in reversed(ancestors):
                rect = getattr(ancestor, "BoundingRectangle", None)
                if rect is None or not in_conversation_list_region(rect):
                    continue
                try:
                    width = max(0, int(rect.width()))
                    height = max(0, int(rect.height()))
                except Exception:
                    continue
                if width >= 140 and height >= max(28, child_height) and height <= 112:
                    return ancestor
            return child

        controls: list[Any] = []
        seen: set[int] = set()

        def walk(control: Any, depth: int = 0, ancestors: list[Any] | None = None) -> None:
            if depth > 12 or len(controls) >= 12:
                return
            parent_chain = ancestors or []
            try:
                children = control.GetChildren()
            except Exception:
                return
            for child in children:
                try:
                    name = str(getattr(child, "Name", "") or "").strip()
                    rect = getattr(child, "BoundingRectangle", None)
                    if (
                        name
                        and len(name) <= 96
                        and rect is not None
                        and in_conversation_list_region(rect)
                        and chat_label_matches(expected, name)
                    ):
                        target = best_click_target(child, parent_chain)
                        target_key = id(target)
                        if target_key not in seen:
                            controls.append(target)
                            seen.add(target_key)
                    walk(child, depth + 1, [*parent_chain, child])
                except Exception:
                    continue

        try:
            walk(self._window)
        except Exception:
            return controls
        return controls

    def _click_control_center(self, control: Any, *, reason: str) -> dict[str, Any]:
        payload = control_payload(control)
        self._ensure_native_foreground(reason)
        try:
            control.Click()
            time.sleep(0.2)
            return {"method": "uia_click", "control": payload}
        except Exception as exc:
            payload["uia_click_error"] = str(exc)

        rect = getattr(control, "BoundingRectangle", None)
        rect_info = rect_payload(rect)
        width = int(rect_info.get("width") or 0)
        height = int(rect_info.get("height") or 0)
        if not width or not height:
            raise BridgeError("visible conversation control has no usable rectangle")
        x = int(rect_info.get("left") or 0) + width // 2
        y = int(rect_info.get("top") or 0) + height // 2
        ctypes.windll.user32.SetCursorPos(x, y)
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(0.2)
        return {
            "method": "coordinate_control_center",
            "control": payload,
            "point": [x, y],
        }

    def _click_screen_point(self, point: Any, *, reason: str) -> dict[str, Any]:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise BridgeError("screen click point is missing or invalid")
        try:
            x = int(point[0])
            y = int(point[1])
        except Exception as exc:
            raise BridgeError(f"screen click point is not numeric: {point}") from exc
        try:
            virtual_left = int(ctypes.windll.user32.GetSystemMetrics(76))
            virtual_top = int(ctypes.windll.user32.GetSystemMetrics(77))
            virtual_width = int(ctypes.windll.user32.GetSystemMetrics(78))
            virtual_height = int(ctypes.windll.user32.GetSystemMetrics(79))
        except Exception:
            virtual_left = 0
            virtual_top = 0
            virtual_width = max(1, int(ctypes.windll.user32.GetSystemMetrics(0)))
            virtual_height = max(1, int(ctypes.windll.user32.GetSystemMetrics(1)))
        virtual_right = virtual_left + max(1, virtual_width)
        virtual_bottom = virtual_top + max(1, virtual_height)
        if x < virtual_left or x >= virtual_right or y < virtual_top or y >= virtual_bottom:
            raise BridgeError(
                "screen click point is outside the virtual desktop: "
                f"point={[x, y]} desktop={[virtual_left, virtual_top, virtual_right, virtual_bottom]}"
            )
        self._ensure_native_foreground(reason)
        ctypes.windll.user32.SetCursorPos(x, y)
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
        time.sleep(0.2)
        return {"method": "coordinate_screen_point", "point": [x, y]}

    def _switch_visible_conversation(self, expected: str) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "contact": str(expected or "").strip(),
            "method": "visible_conversation_click",
            "at": time.time(),
            "success": False,
            "action_success": False,
            "verified": False,
            "candidate_count": 0,
            "candidates": [],
        }
        controls = self._visible_conversation_controls(expected)
        evidence["candidate_count"] = len(controls)
        evidence["candidates"] = [control_payload(control) for control in controls[:4]]
        if not controls:
            evidence["error"] = "no_visible_conversation_candidate"
            evidence["completed_at"] = time.time()
            self._last_contact_switch_evidence = evidence
            return evidence

        try:
            click_result = self._click_control_center(controls[0], reason="visible conversation switch")
        except Exception as exc:
            evidence["error"] = f"visible_conversation_click_failed: {exc}"
            evidence["completed_at"] = time.time()
            self._last_contact_switch_evidence = evidence
            return evidence

        evidence["action_success"] = True
        evidence["click"] = click_result
        time.sleep(self.settle_seconds)
        self._input_control = None
        self._send_button = None
        self._use_coord_fallback = False
        verification = self._target_chat_evidence(expected, allow_recent_switch=False)
        evidence["verification"] = verification
        if verification.get("matched") is True:
            evidence["success"] = True
            evidence["verified"] = True
            self._last_contact = str(expected or "")
        elif verification.get("mismatch") is True:
            evidence["error"] = "target_verification_mismatch"
        else:
            evidence["error"] = "target_verification_unconfirmed"
        evidence["completed_at"] = time.time()
        self._last_contact_switch_evidence = evidence
        return evidence

    def _visual_ocr_reader(self) -> Any:
        cls = type(self)
        if cls._VISUAL_OCR_READER is not None:
            return cls._VISUAL_OCR_READER
        if cls._VISUAL_OCR_ERROR:
            return None
        try:
            import easyocr  # type: ignore

            cls._VISUAL_OCR_READER = easyocr.Reader(["ch_sim", "en"], gpu=False, verbose=False)
        except Exception as exc:
            cls._VISUAL_OCR_ERROR = str(exc)
            return None
        return cls._VISUAL_OCR_READER

    def _visual_header_title_box(self, width: int, height: int) -> tuple[int, int, int, int]:
        x1 = max(220, int(width * 0.30))
        x2 = min(width - 120, int(width * 0.72))
        y1 = max(24, int(height * 0.025))
        y2 = min(height, max(y1 + 44, int(height * 0.10)))
        if x2 <= x1 + 80:
            x2 = min(width, x1 + 260)
        return (x1, y1, x2, y2)

    def _visual_selected_conversation_box(self, image: Any) -> tuple[int, int, int, int] | None:
        try:
            import numpy as np  # type: ignore

            rgb = image.convert("RGB")
            arr = np.array(rgb)
        except Exception:
            return None
        if arr.ndim < 3:
            return None
        height, width = int(arr.shape[0]), int(arr.shape[1])
        if width < 360 or height < 240:
            return None
        x1 = max(58, int(width * 0.06))
        x2 = min(width, max(x1 + 160, int(width * 0.34)))
        y1 = max(64, int(height * 0.07))
        y2 = min(height - 24, int(height * 0.96))
        if x2 <= x1 or y2 <= y1:
            return None
        region = arr[y1:y2, x1:x2, :3]
        red = region[:, :, 0].astype("int16")
        green = region[:, :, 1].astype("int16")
        blue = region[:, :, 2].astype("int16")
        selected_green = (
            (green >= 120)
            & (red <= 80)
            & (blue <= 150)
            & (green >= red * 2)
            & (green >= blue + 35)
        )
        row_counts = selected_green.sum(axis=1)
        threshold = max(24, int((x2 - x1) * 0.22))
        selected_rows = [int(index) for index, count in enumerate(row_counts) if int(count) >= threshold]
        if not selected_rows:
            return None

        groups: list[tuple[int, int]] = []
        start = previous = selected_rows[0]
        for row in selected_rows[1:]:
            if row == previous + 1:
                previous = row
                continue
            groups.append((start, previous))
            start = previous = row
        groups.append((start, previous))
        groups.sort(key=lambda item: (item[1] - item[0], int(row_counts[item[0] : item[1] + 1].sum())), reverse=True)
        best_start, best_end = groups[0]
        if best_end - best_start < 18:
            return None
        top = max(y1, y1 + best_start - 8)
        bottom = min(y2, y1 + best_end + 9)
        return (x1, top, x2, bottom)

    def _read_visual_ocr_crop(self, image: Any, *, label: str, box: tuple[int, int, int, int]) -> dict[str, Any]:
        reader = self._visual_ocr_reader()
        if reader is None:
            return {
                "label": label,
                "box": list(box),
                "texts": [],
                "error": type(self)._VISUAL_OCR_ERROR or "easyocr_unavailable",
            }
        try:
            import numpy as np  # type: ignore

            crop = image.crop(box).convert("RGB")
            raw_results = reader.readtext(np.array(crop), detail=1, paragraph=False)
        except Exception as exc:
            return {"label": label, "box": list(box), "texts": [], "error": str(exc)}

        texts: list[dict[str, Any]] = []
        for item in raw_results:
            try:
                text = str(item[1] or "").strip()
                confidence = float(item[2])
            except Exception:
                continue
            if not text or confidence < VISUAL_OCR_MIN_CONFIDENCE:
                continue
            try:
                bbox = [[int(point[0]), int(point[1])] for point in item[0]]
            except Exception:
                bbox = []
            if bbox:
                xs = [point[0] for point in bbox]
                ys = [point[1] for point in bbox]
                center = [int((min(xs) + max(xs)) / 2), int((min(ys) + max(ys)) / 2)]
            else:
                center = []
            texts.append(
                {
                    "text": text,
                    "confidence": round(confidence, 4),
                    "visual_key": normalize_visual_chat_label(text),
                    "bbox": bbox,
                    "center": center,
                }
            )
        return {"label": label, "box": list(box), "texts": texts}

    def _visual_conversation_list_box(self, width: int, height: int) -> tuple[int, int, int, int]:
        x1 = max(56, int(width * 0.06))
        x2 = min(width, max(x1 + 190, int(width * 0.34)))
        y1 = max(68, int(height * 0.075))
        y2 = min(height - 12, int(height * 0.985))
        if x2 <= x1 + 120:
            x2 = min(width, x1 + 240)
        if y2 <= y1 + 120:
            y2 = min(height, y1 + 420)
        return (x1, y1, x2, y2)

    def _visual_conversation_click_points(
        self,
        *,
        box: tuple[int, int, int, int],
        local_point: list[int],
        win_left: int,
        win_top: int,
    ) -> list[dict[str, Any]]:
        x1, y1, x2, y2 = [int(value) for value in box]
        try:
            text_x = int(local_point[0])
            text_y = int(local_point[1])
        except Exception:
            return []
        row_y = max(y1 + 8, min(text_y, y2 - 8))
        raw_points = [
            ("row_body", x1 + 122, row_y),
            ("row_mid", x1 + 166, row_y),
            ("ocr_text", text_x, row_y),
            ("avatar", x1 + 42, row_y),
        ]
        points: list[dict[str, Any]] = []
        seen: set[tuple[int, int]] = set()
        for label, raw_x, raw_y in raw_points:
            local_x = max(x1 + 8, min(int(raw_x), x2 - 8))
            local_y = max(y1 + 8, min(int(raw_y), y2 - 8))
            screen_point = [int(win_left + local_x), int(win_top + local_y)]
            key = (screen_point[0], screen_point[1])
            if key in seen:
                continue
            points.append({"kind": label, "local_point": [local_x, local_y], "screen_point": screen_point})
            seen.add(key)
        return points

    def _visual_visible_conversation_candidates(self, expected: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "enabled": True,
            "status": "visual_conversation_unavailable",
            "expected_visual_key": normalize_visual_chat_label(expected),
            "candidates": [],
            "crop": {},
        }
        try:
            self.activate()
            result["activation_attempted"] = True
            self._ensure_native_foreground("visual visible conversation OCR")
            result["foreground_verified"] = True
        except Exception as exc:
            result["foreground_verified"] = False
            result["foreground_error"] = str(exc)
            result["status"] = "visual_foreground_unavailable"
            return result
        try:
            win_rect = self._window_rect_payload()
            image = self._grab_window_image()
            width, height = image.size
        except Exception as exc:
            result["error"] = str(exc)
            return result

        box = self._visual_conversation_list_box(int(width), int(height))
        crop_result = self._read_visual_ocr_crop(image, label="conversation_list", box=box)
        result["image_size"] = [int(width), int(height)]
        result["crop"] = crop_result
        result["window_rect"] = dict(win_rect)
        candidates: list[dict[str, Any]] = []
        win_left = int(win_rect.get("left") or 0)
        win_top = int(win_rect.get("top") or 0)
        for text_item in crop_result.get("texts") or []:
            text = str(text_item.get("text") or "")
            if not text or not visual_chat_label_matches(expected, text):
                continue
            center = text_item.get("center")
            if not isinstance(center, list) or len(center) != 2:
                continue
            local_x = box[0] + int(center[0])
            local_y = box[1] + int(center[1])
            click_points = self._visual_conversation_click_points(
                box=box,
                local_point=[local_x, local_y],
                win_left=win_left,
                win_top=win_top,
            )
            candidates.append(
                {
                    "text": text,
                    "confidence": text_item.get("confidence"),
                    "visual_key": text_item.get("visual_key"),
                    "box": list(box),
                    "local_point": [local_x, local_y],
                    "screen_point": [win_left + local_x, win_top + local_y],
                    "click_points": click_points,
                    "bbox": text_item.get("bbox") or [],
                }
            )
        result["candidates"] = candidates
        result["status"] = "matched_visible_conversation" if candidates else "visual_conversation_no_match"
        return result

    def _switch_visual_visible_conversation(self, expected: str) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "contact": str(expected or "").strip(),
            "method": "visual_visible_conversation_click",
            "at": time.time(),
            "success": False,
            "action_success": False,
            "verified": False,
            "candidate_count": 0,
            "candidates": [],
        }
        visual = self._visual_visible_conversation_candidates(expected)
        evidence["visual_status"] = str(visual.get("status") or "")
        evidence["visual"] = visual
        candidates = visual.get("candidates") or []
        evidence["candidate_count"] = len(candidates)
        evidence["candidates"] = candidates[:4]
        if not candidates:
            evidence["error"] = "no_visual_visible_conversation_candidate"
            evidence["completed_at"] = time.time()
            self._last_contact_switch_evidence = evidence
            return evidence

        candidate = candidates[0]
        click_points: list[Any] = []
        if isinstance(candidate, dict):
            raw_click_points = candidate.get("click_points")
            if isinstance(raw_click_points, list):
                click_points = raw_click_points[:4]
            if not click_points and candidate.get("screen_point"):
                click_points = [{"kind": "ocr_text", "screen_point": candidate.get("screen_point")}]
        if not click_points:
            evidence["error"] = "visual_visible_conversation_click_point_missing"
            evidence["completed_at"] = time.time()
            self._last_contact_switch_evidence = evidence
            return evidence

        click_attempts: list[dict[str, Any]] = []
        verification: dict[str, Any] = {}
        for point_item in click_points:
            point = point_item.get("screen_point") if isinstance(point_item, dict) else point_item
            attempt: dict[str, Any] = {
                "kind": str(point_item.get("kind") or "") if isinstance(point_item, dict) else "",
                "point": point,
            }
            try:
                click_result = self._click_screen_point(point, reason="visual visible conversation switch")
            except Exception as exc:
                attempt["error"] = str(exc)
                click_attempts.append(attempt)
                continue

            evidence["action_success"] = True
            evidence.setdefault("click", click_result)
            attempt["click"] = click_result
            time.sleep(self.settle_seconds)
            self._input_control = None
            self._send_button = None
            self._use_coord_fallback = False
            verification = self._target_chat_evidence(expected, allow_recent_switch=False)
            attempt["verification"] = {
                "status": str(verification.get("status") or ""),
                "matched": bool(verification.get("matched")),
                "mismatch": bool(verification.get("mismatch")),
                "observed": str(verification.get("observed") or ""),
            }
            click_attempts.append(attempt)
            if verification.get("matched") is True:
                evidence["success"] = True
                evidence["verified"] = True
                self._last_contact = str(expected or "")
                break
            if verification.get("mismatch") is True:
                break

        evidence["click_attempts"] = click_attempts
        evidence["verification"] = verification
        if evidence.get("success") is not True:
            if verification.get("mismatch") is True:
                evidence["error"] = "target_verification_mismatch"
            elif evidence.get("action_success") is True:
                evidence["error"] = "target_verification_unconfirmed"
            else:
                failures = [str(item.get("error") or "") for item in click_attempts if isinstance(item, dict)]
                evidence["error"] = "visual_visible_conversation_click_failed: " + "; ".join(
                    failure for failure in failures if failure
                )
        evidence["completed_at"] = time.time()
        self._last_contact_switch_evidence = evidence
        return evidence

    def _visual_target_chat_evidence(self, expected: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "enabled": True,
            "matched": False,
            "status": "visual_ocr_unavailable",
            "observed": "",
            "observed_names": [],
            "expected_visual_key": normalize_visual_chat_label(expected),
            "crops": [],
            "visible_conversation_candidates": [],
        }
        try:
            self.activate()
            result["activation_attempted"] = True
            self._ensure_native_foreground("visual target OCR")
            result["foreground_verified"] = True
        except Exception as exc:
            result["foreground_verified"] = False
            result["foreground_error"] = str(exc)
            result["status"] = "visual_foreground_unavailable"
            return result
        try:
            image = self._grab_window_image()
            width, height = image.size
        except Exception as exc:
            result["error"] = str(exc)
            return result

        result["image_size"] = [int(width), int(height)]
        crop_boxes: list[tuple[str, tuple[int, int, int, int]]] = [
            ("header", self._visual_header_title_box(int(width), int(height))),
        ]
        selected_box = self._visual_selected_conversation_box(image)
        if selected_box is not None:
            crop_boxes.append(("selected_conversation", selected_box))

        observed_names: list[str] = []
        for label, box in crop_boxes:
            crop_result = self._read_visual_ocr_crop(image, label=label, box=box)
            result["crops"].append(crop_result)
            for text_item in crop_result.get("texts") or []:
                text = str(text_item.get("text") or "")
                if not text:
                    continue
                observed_names.append(text)
                if visual_chat_label_matches(expected, text):
                    result.update(
                        {
                            "matched": True,
                            "status": f"matched_by_visual_{label}",
                            "observed": text,
                            "matched_crop": label,
                            "observed_names": observed_names,
                        }
                    )
                    return result

        result["observed_names"] = observed_names
        visible_conversation = self._visual_visible_conversation_candidates(expected)
        result["visible_conversation"] = visible_conversation
        result["visible_conversation_candidates"] = visible_conversation.get("candidates") or []
        if result["visible_conversation_candidates"]:
            result["status"] = "visual_ocr_visible_conversation_only"
            return result
        result["status"] = "visual_ocr_no_match" if observed_names else "visual_ocr_no_text"
        return result

    def _target_evidence_blocker(self, expected: str, phase: str) -> dict[str, Any]:
        evidence = self._target_chat_evidence(expected)
        self._last_target_evidence = dict(evidence)
        if evidence.get("mismatch") is True:
            raise BridgeError(target_chat_mismatch_message(evidence, phase))
        if evidence.get("matched") is not True:
            raise BridgeError(target_chat_unconfirmed_message(evidence, phase))
        return evidence

    def _retry_unconfirmed_target_evidence(self, expected: str, evidence: dict[str, Any]) -> dict[str, Any]:
        if evidence.get("matched") is True or evidence.get("mismatch") is True:
            return evidence
        if not expected:
            return evidence

        retry_results: list[dict[str, Any]] = []
        for delay_seconds in (0.35, 0.75):
            time.sleep(delay_seconds)
            try:
                self._ensure_native_foreground("target evidence retry")
            except Exception:
                pass
            refreshed = self._target_chat_evidence(expected)
            retry_results.append(
                {
                    "delay_seconds": delay_seconds,
                    "status": str(refreshed.get("status") or ""),
                    "matched": bool(refreshed.get("matched")),
                    "mismatch": bool(refreshed.get("mismatch")),
                    "observed": str(refreshed.get("observed") or ""),
                }
            )
            if refreshed.get("matched") is True or refreshed.get("mismatch") is True:
                refreshed["retry_from"] = {
                    "status": str(evidence.get("status") or ""),
                    "observed": str(evidence.get("observed") or ""),
                }
                refreshed["retry_attempts"] = retry_results
                return refreshed

        evidence["retry_attempts"] = retry_results
        return evidence

    def _ensure_target_chat_ready(self, expected: str, phase: str) -> dict[str, Any]:
        expected = str(expected or "").strip()
        if getattr(self, "_assume_target_confirmed", False):
            # 操作者已人工确认当前聊天即目标；跳过 UIA/视觉核实直接放行（仅手动监督路径）。
            evidence = {
                "expected": expected,
                "matched": True,
                "mismatch": False,
                "status": "assumed_confirmed_by_operator",
                "observed": expected,
                "targeting_action": "operator_assumed",
            }
            self._last_target_evidence = dict(evidence)
            if expected:
                self._last_contact = expected
            return evidence
        evidence = self._target_chat_evidence(expected)
        evidence = self._retry_unconfirmed_target_evidence(expected, evidence)
        self._last_target_evidence = dict(evidence)
        if evidence.get("matched") is True:
            evidence["targeting_action"] = "reuse_current_chat"
            if expected:
                self._last_contact = expected
            self._last_target_evidence = dict(evidence)
            return evidence

        should_switch = False
        if self.search_enabled and expected:
            should_switch = (
                evidence.get("mismatch") is True
                or expected != self._last_contact
                or self._target_evidence_needs_contact_refresh(evidence)
            )
        if should_switch:
            switch_evidence = self._switch_contact(expected)
            evidence = self._target_chat_evidence(expected)
            evidence = self._retry_unconfirmed_target_evidence(expected, evidence)
            evidence["targeting_action"] = "search_switch"
            evidence["switch_evidence"] = switch_evidence
        elif expected and not self.search_enabled and evidence.get("matched") is not True:
            if not getattr(self, "visible_conversation_switch_enabled", True):
                evidence["targeting_action"] = "blocked_current_window_only"
                evidence["switch_evidence"] = {
                    "method": "current_window_only",
                    "action_success": False,
                    "error": "visible_conversation_switch_disabled",
                }
            else:
                switch_evidence = self._switch_visible_conversation(expected)
                if switch_evidence.get("action_success") is True:
                    refreshed = switch_evidence.get("verification")
                    evidence = dict(refreshed) if isinstance(refreshed, dict) else self._target_chat_evidence(expected)
                    evidence = self._retry_unconfirmed_target_evidence(expected, evidence)
                    evidence["targeting_action"] = "visible_conversation_switch"
                    evidence["switch_evidence"] = switch_evidence
                elif getattr(self, "visual_target_enabled", False):
                    visual_switch_evidence = self._switch_visual_visible_conversation(expected)
                    if visual_switch_evidence.get("action_success") is True:
                        refreshed = visual_switch_evidence.get("verification")
                        evidence = dict(refreshed) if isinstance(refreshed, dict) else self._target_chat_evidence(expected)
                        evidence = self._retry_unconfirmed_target_evidence(expected, evidence)
                        evidence["targeting_action"] = "visual_visible_conversation_switch"
                        evidence["switch_evidence"] = visual_switch_evidence
                        evidence["uia_switch_evidence"] = switch_evidence
                    else:
                        evidence["targeting_action"] = "blocked_search_disabled"
                        evidence["switch_evidence"] = switch_evidence
                        evidence["visual_switch_evidence"] = visual_switch_evidence
                else:
                    evidence["targeting_action"] = "blocked_search_disabled"
                    evidence["switch_evidence"] = switch_evidence

        self._last_target_evidence = dict(evidence)
        if evidence.get("mismatch") is True:
            raise BridgeError(target_chat_mismatch_message(evidence, phase))
        if evidence.get("matched") is not True:
            raise BridgeError(target_chat_unconfirmed_message(evidence, phase))
        return evidence

    def _target_evidence_needs_contact_refresh(self, evidence: Any) -> bool:
        if not isinstance(evidence, dict):
            return True
        if evidence.get("matched") is True or evidence.get("mismatch") is True:
            return False
        return str(evidence.get("status") or "") in {
            "unavailable",
            "no_header_candidates",
            "no_strong_title_candidate",
        }

    def target_probe(self, expected: str) -> dict[str, Any]:
        activation: dict[str, Any] = {"attempted": True, "ok": False, "error": ""}
        try:
            self.activate()
            activation["ok"] = True
        except Exception as exc:
            activation["error"] = str(exc)
        evidence = self._target_chat_evidence(expected)
        return {
            "automation": "uia_wechat4",
            "read_only": True,
            "activation": activation,
            "expected": str(expected or "").strip(),
            "target_evidence": evidence,
            "matched": bool(evidence.get("matched")) if isinstance(evidence, dict) else False,
            "mismatch": bool(evidence.get("mismatch")) if isinstance(evidence, dict) else False,
            "status": str(evidence.get("status") or "unknown") if isinstance(evidence, dict) else "unknown",
            "observed": str(evidence.get("observed") or "") if isinstance(evidence, dict) else "",
        }

    def probe(self) -> dict[str, Any]:
        if not self._ensure_window():
            raise BridgeError("Weixin main window was not found by UIA")
        rect = self._window_rect_payload()
        locate_error = ""
        input_located = False
        class_name = self._window_class_name()
        login_required = self._window_login_required(self._window)
        if not login_required:
            try:
                input_located = bool(self._locate_input())
            except Exception as exc:
                locate_error = str(exc)
        else:
            locate_error = login_required_error(class_name)
        return {
            "automation": "uia_wechat4",
            "title": str(self._window.Name or ""),
            "class": class_name,
            "is_electron": bool(self._is_electron),
            "login_required": login_required,
            "native_hwnd": self._find_native_hwnd(),
            "foreground_helper": "AttachThreadInput+foreground-thread+topmost-retry",
            "rect": [rect["left"], rect["top"], rect["right"], rect["bottom"]],
            "rect_source": str(rect.get("source") or ""),
            "size": [rect["width"], rect["height"]],
            "input_located": input_located,
            "input_mode": self._input_mode(),
            "input_control_found": self._input_control is not None,
            "send_button_found": self._send_button is not None,
            "uses_coord_fallback": bool(self._use_coord_fallback),
            "input_control": control_payload(self._input_control),
            "send_button": control_payload(self._send_button),
            "locate_error": locate_error,
            "window_scan": getattr(self, "_last_window_scan", {})
            if isinstance(getattr(self, "_last_window_scan", {}), dict)
            else {},
            "capabilities": {
                "text_out": "blocked_login_required" if login_required else ("ready" if input_located else "blocked_input_not_found"),
                "image_out": "blocked_login_required" if login_required else ("clipboard_uia" if input_located else "blocked_input_not_found"),
                "animated_sticker_out": "blocked_login_required" if login_required else ("ready-live-proven-via-uia-file-drop-gif" if input_located else "blocked_input_not_found"),
                "voice_out": "blocked_login_required" if login_required else ("ready-native-bubble-uia-content-route-not-proven" if input_located else "blocked_input_not_found"),
                "realtime_voice": "unsupported",
            },
        }

    def capture(self, path: Path) -> Path:
        self.activate()
        path.parent.mkdir(parents=True, exist_ok=True)
        image = self._grab_window_image()
        image.save(path)
        return path

    def _grab_window_image(self) -> Any:
        try:
            from PIL import ImageGrab  # type: ignore
        except ImportError as exc:
            raise BridgeError("capture needs Pillow installed") from exc
        if not self._ensure_window():
            raise BridgeError("Weixin main window was not found by UIA")
        rect = self._window_rect_payload()
        bbox = (
            int(rect.get("left") or 0),
            int(rect.get("top") or 0),
            int(rect.get("right") or 0),
            int(rect.get("bottom") or 0),
        )
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            raise BridgeError(f"window capture has invalid bbox: {list(bbox)}")
        return ImageGrab.grab(bbox=bbox)

    def activate(self) -> None:
        if not self._ensure_window():
            raise BridgeError("Weixin main window was not found by UIA")
        self._try_uia_activate_window()
        self._force_native_foreground()
        time.sleep(0.3)

    def _try_uia_activate_window(self) -> None:
        if self._window is None:
            return
        try:
            self._window.SetActive()
        except Exception:
            pass
        try:
            self._window.SwitchToThisWindow()
        except Exception:
            pass

    def _switch_contact(self, contact: str) -> dict[str, Any]:
        self.activate()
        self._raise_if_login_required()
        evidence: dict[str, Any] = {
            "contact": str(contact or ""),
            "method": "native_search",
            "at": time.time(),
            "success": False,
            "action_success": False,
            "verified": False,
        }
        action_success = False
        if self._native_search_contact(contact):
            action_success = True
        else:
            evidence["method"] = "keyboard_search"
            self.auto.SendKeys("{Ctrl}f")
            time.sleep(0.25)
            self.auto.SendKeys("{Ctrl}a")
            self.pyperclip.copy(contact)
            time.sleep(0.08)
            self.auto.SendKeys("{Ctrl}v")
            time.sleep(self.settle_seconds)
            self.auto.SendKeys("{Enter}")
            action_success = True
        time.sleep(self.settle_seconds)
        self._input_control = None
        self._send_button = None
        self._use_coord_fallback = False
        evidence["action_success"] = action_success
        if action_success:
            verification = self._target_chat_evidence(contact, allow_recent_switch=False)
            evidence["verification"] = verification
            if verification.get("matched") is True:
                evidence["success"] = True
                evidence["verified"] = True
                self._last_contact = str(contact or "")
            elif verification.get("mismatch") is True:
                evidence["error"] = "target_verification_mismatch"
            else:
                evidence["error"] = "target_verification_unconfirmed"
        else:
            evidence["error"] = "search_action_failed"
        evidence["completed_at"] = time.time()
        self._last_contact_switch_evidence = evidence
        return evidence

    def _find_native_hwnd(self) -> int:
        try:
            import ctypes
        except Exception:
            return 0
        user32 = ctypes.windll.user32
        handles: set[int] = set()
        try:
            native_handle = int(getattr(self._window, "NativeWindowHandle", 0) or 0)
            if native_handle:
                handles.add(native_handle)
        except Exception:
            pass
        for class_name in self.NATIVE_WINDOW_CLASSES:
            try:
                hwnd = int(user32.FindWindowW(class_name, None) or 0)
            except Exception:
                hwnd = 0
            if hwnd:
                handles.add(hwnd)

        try:
            from ctypes import wintypes

            enum_proc_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

            @enum_proc_type
            def enum_proc(hwnd: Any, _lparam: Any) -> bool:
                handles.add(int(hwnd))
                return True

            user32.EnumWindows(enum_proc, 0)
        except Exception:
            pass

        candidates: list[tuple[tuple[int, int, int, int, int], int]] = []
        for hwnd in handles:
            try:
                title_length = int(user32.GetWindowTextLengthW(hwnd) or 0)
                title_buffer = ctypes.create_unicode_buffer(title_length + 1)
                user32.GetWindowTextW(hwnd, title_buffer, title_length + 1)
                title = str(title_buffer.value or "")
                class_buffer = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, class_buffer, 256)
                class_name = str(class_buffer.value or "")
            except Exception:
                title = ""
                class_name = ""
            if class_name in self.EXCLUDE_CLASSES:
                continue
            title_matches = title in {"微信", "WeChat", "Weixin"}
            class_matches = class_name in self.NATIVE_WINDOW_CLASSES
            process_matches = bool(native_window_process_identity(hwnd).get("matches_wechat"))
            if not title_matches and not class_matches and not process_matches:
                continue
            try:
                from ctypes import wintypes

                rect = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                left = int(rect.left)
                top = int(rect.top)
                width = max(0, int(rect.right - rect.left))
                height = max(0, int(rect.bottom - rect.top))
            except Exception:
                left = top = width = height = 0
            if not width or not height:
                continue
            visible = bool(user32.IsWindowVisible(hwnd))
            iconic = bool(user32.IsIconic(hwnd))
            minimized_rect = left <= -30000 or top <= -30000
            area = width * height
            candidates.append(
                (
                    (
                        0 if title_matches else 1,
                        0 if visible else 1,
                        0 if not iconic and not minimized_rect else 1,
                        -area,
                        hwnd,
                    ),
                    hwnd,
                )
            )
        if not candidates:
            return 0
        candidates.sort(key=lambda item: item[0])
        return int(candidates[0][1])

    def _native_window_pid(self, hwnd: int) -> int:
        return native_window_pid(hwnd)

    def _foreground_belongs_to_wechat(self) -> bool:
        try:
            import ctypes
        except Exception:
            return False
        hwnd = self._find_native_hwnd()
        if not hwnd:
            return False
        try:
            foreground = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        except Exception:
            foreground = 0
        if not foreground:
            return False
        if foreground == hwnd:
            return True
        try:
            if ctypes.windll.user32.IsChild(hwnd, foreground):
                return True
        except Exception:
            pass
        try:
            if int(ctypes.windll.user32.GetAncestor(foreground, self.GA_ROOT) or 0) == hwnd:
                return True
        except Exception:
            pass
        wechat_pid = self._native_window_pid(hwnd)
        foreground_pid = self._native_window_pid(foreground)
        return bool(wechat_pid and foreground_pid and wechat_pid == foreground_pid)

    def _ensure_native_foreground(self, context: str) -> None:
        if not self._ensure_window():
            raise BridgeError("Weixin main window was not found by UIA")
        self._raise_if_login_required()
        if self._foreground_belongs_to_wechat():
            return
        for attempt in range(self.NATIVE_FOREGROUND_ATTEMPTS):
            if attempt and not self._ensure_window():
                raise BridgeError("Weixin main window was not found by UIA")
            if attempt == 1:
                self._try_uia_activate_window()
            self._force_native_foreground()
            time.sleep(0.08 + min(attempt, 2) * 0.08)
            self._raise_if_login_required()
            if self._foreground_belongs_to_wechat():
                return
        raise BridgeError(
            f"wechat_not_foreground: refused {context}; foreground window is not PC WeChat "
            f"after {self.NATIVE_FOREGROUND_ATTEMPTS} activation attempts"
        )

    def _force_native_foreground(self) -> bool:
        try:
            import ctypes
        except Exception:
            return False
        hwnd = self._find_native_hwnd()
        if not hwnd:
            return False
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        target_tid = 0
        foreground_tid = 0
        current_tid = 0
        attached_threads: list[int] = []

        def attach_thread(tid: int) -> None:
            if not tid or not current_tid or tid == current_tid or tid in attached_threads:
                return
            try:
                if user32.AttachThreadInput(current_tid, tid, True):
                    attached_threads.append(tid)
            except Exception:
                pass

        try:
            foreground = int(user32.GetForegroundWindow() or 0)
            target_tid = int(user32.GetWindowThreadProcessId(hwnd, None) or 0)
            if foreground:
                foreground_tid = int(user32.GetWindowThreadProcessId(foreground, None) or 0)
            current_tid = int(kernel32.GetCurrentThreadId() or 0)
            attach_thread(foreground_tid)
            attach_thread(target_tid)
            try:
                user32.AllowSetForegroundWindow(-1)
            except Exception:
                pass
            try:
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, self.SW_RESTORE)
                else:
                    user32.ShowWindow(hwnd, self.SW_SHOW)
            except Exception:
                user32.ShowWindow(hwnd, self.SW_RESTORE)
            flags = self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_SHOWWINDOW
            try:
                user32.SetWindowPos(hwnd, self.HWND_TOPMOST, 0, 0, 0, 0, flags)
                user32.SetWindowPos(hwnd, self.HWND_NOTOPMOST, 0, 0, 0, 0, flags)
            except Exception:
                pass
            try:
                user32.SwitchToThisWindow(hwnd, True)
            except Exception:
                pass
            for action in (user32.BringWindowToTop, user32.SetActiveWindow, user32.SetFocus, user32.SetForegroundWindow):
                try:
                    action(hwnd)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            for tid in reversed(attached_threads):
                try:
                    user32.AttachThreadInput(current_tid, tid, False)
                except Exception:
                    pass
        return self._foreground_belongs_to_wechat()

    def _native_key(self, vk: int) -> bool:
        self._ensure_native_foreground("native key input")
        try:
            import ctypes
            ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
            ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
            return True
        except Exception:
            return False

    def _native_hotkey(self, *keys: int) -> bool:
        self._ensure_native_foreground("native hotkey input")
        try:
            import ctypes
            user32 = ctypes.windll.user32
            for vk in keys:
                user32.keybd_event(vk, 0, 0, 0)
            for vk in reversed(keys):
                user32.keybd_event(vk, 0, 2, 0)
            return True
        except Exception:
            return False

    def _native_search_contact(self, contact: str) -> bool:
        self._force_native_foreground()
        if not self._native_hotkey(0x11, 0x46):
            return False
        time.sleep(0.5)
        if not self._native_hotkey(0x11, 0x41):
            return False
        time.sleep(0.15)
        self.pyperclip.copy(contact)
        time.sleep(0.1)
        if not self._native_hotkey(0x11, 0x56):
            return False
        time.sleep(0.3)
        return self._native_key(0x0D)

    def _locate_input(self) -> bool:
        if not self._ensure_window():
            return False
        if self._login_required():
            return False
        if self._input_control is not None:
            try:
                if self._input_control.Exists(0.1):
                    return True
            except Exception:
                self._input_control = None
                self._send_button = None

        win_rect = self._window_rect_payload()
        win_center_y = int(win_rect.get("top") or 0) + int(win_rect.get("height") or 0) / 2
        edits: list[Any] = []

        def walk(control: Any, depth: int = 0) -> None:
            if depth > 14:
                return
            try:
                children = control.GetChildren()
            except Exception:
                return
            for child in children:
                try:
                    if child.ControlTypeName == "EditControl":
                        edits.append(child)
                    walk(child, depth + 1)
                except Exception:
                    continue

        walk(self._window)
        if not edits:
            self._use_coord_fallback = True
            return True

        candidates = [
            edit
            for edit in edits
            if edit.BoundingRectangle
            and edit.BoundingRectangle.top >= win_center_y - 20
            and edit.BoundingRectangle.width() > 100
        ]
        if not candidates:
            candidates = [edit for edit in edits if edit.BoundingRectangle]
        if not candidates:
            self._use_coord_fallback = True
            return True

        candidates.sort(
            key=lambda edit: edit.BoundingRectangle.width() * edit.BoundingRectangle.height(),
            reverse=True,
        )
        self._input_control = candidates[0]
        self._send_button = self._find_send_button()
        self._use_coord_fallback = False
        return True

    def _find_send_button(self) -> Any:
        buttons: list[Any] = []

        def walk(control: Any, depth: int = 0) -> None:
            if depth > 8:
                return
            try:
                children = control.GetChildren()
            except Exception:
                return
            for child in children:
                try:
                    if child.ControlTypeName == "ButtonControl":
                        name = str(child.Name or "")
                        if "发送" in name or "Send" in name or not name.strip():
                            buttons.append(child)
                    walk(child, depth + 1)
                except Exception:
                    continue

        try:
            walk(self._window)
        except Exception:
            pass
        return buttons[0] if buttons else None

    def _input_mode(self) -> str:
        if self._use_coord_fallback:
            return "coordinate_fallback"
        if self._input_control is not None:
            try:
                if getattr(self._input_control, "IsValuePatternAvailable", False):
                    return "direct_uia_value_pattern"
            except Exception:
                pass
            return "direct_uia_sendkeys"
        return "unknown"

    def _execution_profile(self) -> dict[str, Any]:
        return {
            "input_mode": self._input_mode(),
            "uses_coord_fallback": bool(self._use_coord_fallback),
            "input_control_found": self._input_control is not None,
            "send_button_found": self._send_button is not None,
            "input_control": control_payload(self._input_control),
            "send_button": control_payload(self._send_button),
        }

    def _click_window_fraction(self, x_fraction: float, y_fraction: float) -> None:
        if not self._ensure_window():
            raise BridgeError("Weixin main window was not found by UIA")
        self._ensure_native_foreground("coordinate click")
        rect = self._window_rect_payload()
        width = int(rect.get("width") or 0)
        height = int(rect.get("height") or 0)
        if not width or not height:
            raise BridgeError("Weixin main window has no usable rectangle for coordinate input")
        x = int(rect.get("left") or 0) + int(width * x_fraction)
        y = int(rect.get("top") or 0) + int(height * y_fraction)
        try:
            import ctypes

            ctypes.windll.user32.SetCursorPos(x, y)
            ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
            ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
            time.sleep(0.2)
        except Exception:
            pass

    def _focus_input_area(self) -> None:
        self._click_window_fraction(COMPOSE_X, COMPOSE_Y)

    def _paste_clipboard_into_input(self) -> None:
        if self._use_coord_fallback or self._input_control is None:
            self._focus_input_area()
            self._ensure_native_foreground("clipboard paste")
            self.auto.SendKeys("{Ctrl}v")
            return
        self._input_control.SendKeys("{Ctrl}v")

    def _clear_input_for_media(self) -> None:
        if self._use_coord_fallback or self._input_control is None:
            self._focus_input_area()
            self._ensure_native_foreground("media compose clear")
            self.auto.SendKeys("{Ctrl}a")
            self.auto.SendKeys("{Delete}")
            time.sleep(0.05)
            return
        if getattr(self._input_control, "IsValuePatternAvailable", False):
            try:
                self._input_control.SetValue("")
                time.sleep(0.05)
                return
            except Exception:
                pass
        self._input_control.SendKeys("{Ctrl}a")
        self._input_control.SendKeys("{Delete}")
        time.sleep(0.05)

    def _submit_input(self) -> str:
        if self.submit_method == "button" and self._send_button is not None:
            try:
                self._send_button.Click()
                return "button"
            except Exception:
                pass
        if self._use_coord_fallback or self._input_control is None:
            if self.submit_method == "button":
                used = []
                for x_fraction, y_fraction in SEND_BUTTON_FALLBACK_POINTS:
                    self._click_window_fraction(x_fraction, y_fraction)
                    used.append(f"{x_fraction:.3f},{y_fraction:.3f}")
                    time.sleep(0.1)
                return "button_coordinate_fallback:" + ";".join(used)
            self._focus_input_area()
            self._ensure_native_foreground("submit enter")
            self.auto.SendKeys("{Enter}")
            return "enter_coordinate_fallback"
        else:
            self._input_control.SendKeys("{Enter}")
            return "enter_uia_control"

    def _submit_input_alternate(self, first_used: str) -> str:
        first_used = first_used.lower()
        if first_used.startswith("button"):
            if self._use_coord_fallback or self._input_control is None:
                self._focus_input_area()
                self._ensure_native_foreground("retry submit enter")
                self.auto.SendKeys("{Enter}")
                return "retry_enter_coordinate_fallback"
            self._input_control.SendKeys("{Enter}")
            return "retry_enter_uia_control"

        if self._send_button is not None:
            try:
                self._send_button.Click()
                return "retry_button"
            except Exception:
                pass
        used = []
        for x_fraction, y_fraction in SEND_BUTTON_FALLBACK_POINTS:
            self._click_window_fraction(x_fraction, y_fraction)
            used.append(f"{x_fraction:.3f},{y_fraction:.3f}")
            time.sleep(0.1)
        return "retry_button_coordinate_fallback:" + ";".join(used)

    def _copy_input_text(self) -> str:
        sentinel = f"__wechat_agent_empty_clipboard_{time.time_ns()}__"
        try:
            previous_clipboard = str(self.pyperclip.paste() or "")
        except Exception:
            previous_clipboard = ""
        try:
            self.pyperclip.copy(sentinel)
            if self._use_coord_fallback or self._input_control is None:
                self._focus_input_area()
                self._ensure_native_foreground("compose copy")
                self.auto.SendKeys("{Ctrl}a")
                self.auto.SendKeys("{Ctrl}c")
            else:
                self._input_control.SendKeys("{Ctrl}a")
                self._input_control.SendKeys("{Ctrl}c")
            time.sleep(0.1)
            copied = str(self.pyperclip.paste() or "")
            return "" if copied == sentinel else copied
        finally:
            try:
                self.pyperclip.copy(previous_clipboard)
            except Exception:
                pass

    def _verify_visible_outgoing_bubble(self, before_image: Any | None, *, evidence_id: str = "") -> dict[str, Any]:
        if before_image is None:
            return {"verified": False, "error": "missing_before_image"}
        try:
            after_image = self._grab_window_image()
        except Exception as exc:
            return {"verified": False, "error": f"after_capture_failed: {exc}"}
        result = outgoing_green_bubble_delta(before_image, after_image)
        if evidence_id:
            result["captures"] = {
                "before": save_capture_image(before_image, item_id=evidence_id, phase="before"),
                "after": save_capture_image(after_image, item_id=evidence_id, phase="after"),
            }
        return result

    def _verify_visible_outgoing_media(self, before_image: Any | None, *, evidence_id: str = "") -> dict[str, Any]:
        if before_image is None:
            return {"verified": False, "error": "missing_before_image"}
        after_image = None
        result: dict[str, Any] = {}
        pending_seen = False
        max_checks_raw = os.environ.get("WECHAT_PC_MEDIA_SEND_STATUS_CHECKS", "").strip()
        try:
            max_checks = int(max_checks_raw) if max_checks_raw else MEDIA_SEND_STATUS_MAX_CHECKS
        except ValueError:
            max_checks = MEDIA_SEND_STATUS_MAX_CHECKS
        max_checks = max(1, min(240, max_checks))
        for attempt in range(max_checks):
            try:
                after_image = self._grab_window_image()
            except Exception as exc:
                return {"verified": False, "error": f"after_capture_failed: {exc}"}
            result = outgoing_media_delta(before_image, after_image)
            result["status_check_attempts"] = attempt + 1
            delivery_status = result.get("delivery_status") if isinstance(result, dict) else {}
            state = str(delivery_status.get("state") if isinstance(delivery_status, dict) else "")
            if result.get("verified"):
                break
            if result.get("media_visible") and state == "pending":
                pending_seen = True
                if attempt + 1 < max_checks:
                    time.sleep(MEDIA_SEND_STATUS_POLL_SECONDS)
                    continue
            break
        if pending_seen:
            result["pending_indicator_seen"] = True
        if result.get("media_visible") and not result.get("verified"):
            delivery_status = result.get("delivery_status")
            if isinstance(delivery_status, dict) and delivery_status.get("state") == "pending":
                result["error"] = "outgoing media is still pending in WeChat"
            elif isinstance(delivery_status, dict) and delivery_status.get("state") == "failed":
                result["error"] = "outgoing media shows a WeChat failure marker"
        if evidence_id:
            result["captures"] = {
                "before": save_capture_image(before_image, item_id=evidence_id, phase="media-before"),
                "after": save_capture_image(after_image, item_id=evidence_id, phase="media-after"),
            }
        return result

    def _verify_visible_media_preview(self, before_image: Any | None, *, evidence_id: str = "") -> dict[str, Any]:
        if before_image is None:
            return {"verified": False, "error": "missing_before_image"}
        try:
            after_image = self._grab_window_image()
        except Exception as exc:
            return {"verified": False, "error": f"after_capture_failed: {exc}"}
        result = media_preview_delta(before_image, after_image)
        if evidence_id:
            result["captures"] = {
                "before": save_capture_image(before_image, item_id=evidence_id, phase="media-preview-before"),
                "after": save_capture_image(after_image, item_id=evidence_id, phase="media-preview-after"),
            }
        return result

    def _cancel_voice_recording_mode(self) -> str:
        self._click_window_fraction(VOICE_CANCEL_BUTTON_X, VOICE_CANCEL_BUTTON_Y)
        return f"voice_cancel_coordinate:{VOICE_CANCEL_BUTTON_X:.3f},{VOICE_CANCEL_BUTTON_Y:.3f}"

    def _start_native_voice_recording(self) -> str:
        self._click_window_fraction(VOICE_START_BUTTON_X, VOICE_START_BUTTON_Y)
        return f"voice_start_coordinate:{VOICE_START_BUTTON_X:.3f},{VOICE_START_BUTTON_Y:.3f}"

    def _submit_native_voice_recording(self) -> str:
        self._click_window_fraction(VOICE_SEND_BUTTON_X, VOICE_SEND_BUTTON_Y)
        return f"voice_send_coordinate:{VOICE_SEND_BUTTON_X:.3f},{VOICE_SEND_BUTTON_Y:.3f}"

    def send_voice(
        self,
        chat: str,
        voice_text: str = "",
        *,
        audio_path: str = "",
        duration_seconds: float = VOICE_DEFAULT_RECORD_SECONDS,
        evidence_id: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            audio_path = str(audio_path or "").strip()
            audio_duration = audio_file_duration_seconds(audio_path)
            duration = voice_record_seconds_for_audio(
                duration_seconds,
                audio_duration_seconds=audio_duration,
            )
            audio_route = native_voice_route_status(audio_path)
            self.activate()
            self._raise_if_login_required()
            target_before = self._ensure_target_chat_ready(chat, "before_send")
            if not self._locate_input():
                raise BridgeError("chat input was not found by UIA")

            visual_before = self._grab_window_image()
            submit_attempts: list[str] = []
            recording_started = False
            playback: dict[str, Any] = {}
            playback_prepared: dict[str, Any] = {}
            playback_stop: dict[str, Any] = {}
            audio_route_session: dict[str, Any] = {}
            audio_route_restore: dict[str, Any] = {}
            try:
                if audio_path:
                    audio_route_session = begin_native_voice_audio_route(audio_path, require_content_proven=True)
                    if not audio_route_session.get("ok"):
                        raise BridgeError(
                            "native voice audio route failed: "
                            f"{audio_route_session.get('error') or 'unknown error'}"
                        )
                    # Do the slow decode/resample + warm the output stream BEFORE the
                    # recording starts, so the bubble does not open with dead air whose
                    # length scales with the clip (see leading-silence fix 2026-06-17).
                    playback_prepared = prepare_native_voice_audio_playback(
                        audio_path, route_session=audio_route_session
                    )
                    if playback_prepared.get("mode") == "none" and not playback_prepared.get("started", True):
                        raise BridgeError(
                            f"native voice TTS playback preparation failed: {playback_prepared.get('error') or 'unknown error'}"
                        )
                submit_attempts.append(self._start_native_voice_recording())
                recording_started = True
                if audio_path:
                    time.sleep(VOICE_TTS_PLAYBACK_DELAY_SECONDS)
                    playback = play_prepared_native_voice_audio_playback(playback_prepared)
                    if not playback.get("started"):
                        raise BridgeError(f"native voice TTS playback failed: {playback.get('error') or 'unknown error'}")
                time.sleep(duration)
                submit_attempts.append(self._submit_native_voice_recording())
                recording_started = False
                time.sleep(max(self.settle_seconds, 1.0))
            except Exception:
                if recording_started:
                    try:
                        submit_attempts.append(self._cancel_voice_recording_mode())
                    except Exception:
                        pass
                raise
            finally:
                if audio_path and playback.get("started"):
                    playback_stop = stop_native_voice_audio_playback()
                if audio_route_session.get("attempted") or audio_route_session.get("ok"):
                    audio_route_restore = restore_native_voice_audio_route(audio_route_session)

            visual_delta = self._verify_visible_outgoing_bubble(visual_before, evidence_id=evidence_id)
            if not visual_delta.get("verified"):
                self._raise_if_login_required()
                raise BridgeError("native voice send did not create a visible outgoing voice bubble; not acking")
            target_after = self._target_chat_evidence(chat)
            return {
                "verified": True,
                "automation": "uia_wechat4",
                "chat": chat,
                "target_evidence": {
                    "expected": chat,
                    "before_send": target_before,
                    "after_send": target_after,
                    "matched": bool(target_before.get("matched") or target_after.get("matched")),
                    "mismatch": bool(target_before.get("mismatch") or target_after.get("mismatch")),
                },
                "native_voice_bubble": True,
                "voice_delivery": "native_recording_uia_coordinate",
                "record_seconds": round(duration, 3),
                "voice_text_chars": len(str(voice_text or "")),
                "submit_used": " -> ".join(submit_attempts),
                "submit_attempts": submit_attempts,
                "submit_attempt_count": len(submit_attempts),
                "message_visible_verified": bool(visual_delta.get("verified")),
                "delivery_verified": "visible_outgoing_bubble" if visual_delta.get("verified") else "",
                "visual_delta": visual_delta,
                "audio_source": (
                    "tts_audio_playback_to_windows_default_output"
                    if playback.get("started")
                    else "wechat_current_microphone_input"
                ),
                "audio_routing_note": (
                    "TTS playback started; WeChat still records from its selected microphone, so content depends on Windows audio routing."
                    if playback.get("started")
                    else ""
                ),
                "tts_audio_path": audio_path,
                "tts_audio_duration_seconds": round(audio_duration, 3) if audio_duration else 0.0,
                "audio_route": audio_route,
                "audio_route_session": compact_native_voice_route_session(audio_route_session),
                "audio_route_restore": compact_native_voice_route_restore(audio_route_restore),
                "tts_playback": playback,
                "tts_playback_stop": playback_stop,
                **self._execution_profile(),
            }

    def send_text(self, chat: str, text: str, *, evidence_id: str = "") -> dict[str, Any]:
        with self._lock:
            if not text.strip():
                raise BridgeError("text message is empty")
            self.activate()
            self._raise_if_login_required()
            target_before = self._ensure_target_chat_ready(chat, "before_send")
            if not self._locate_input():
                raise BridgeError("chat input was not found by UIA")

            visual_before = self._grab_window_image() if self._use_coord_fallback or self._input_control is None else None
            if self._use_coord_fallback or self._input_control is None:
                self.pyperclip.copy(text)
                self._focus_input_area()
                self._ensure_native_foreground("text paste")
                self.auto.SendKeys("{Ctrl}a")
                self.auto.SendKeys("{Ctrl}v")
            elif getattr(self._input_control, "IsValuePatternAvailable", False):
                try:
                    self._input_control.SetValue("")
                    time.sleep(0.03)
                    self._input_control.SetValue(text)
                except Exception:
                    self.pyperclip.copy(text)
                    self._input_control.SendKeys("{Ctrl}a")
                    self._input_control.SendKeys("{Ctrl}v")
            else:
                self.pyperclip.copy(text)
                self._input_control.SendKeys("{Ctrl}a")
                self._input_control.SendKeys("{Ctrl}v")

            time.sleep(0.15)
            compose_before = self._copy_input_text()
            if compose_before and not compose_still_contains_submitted_text(text, compose_before):
                raise BridgeError("compose verification failed before send; pasted text was not found")
            submit_used = self._submit_input()
            submit_attempts = [submit_used]
            retry_reason = ""
            time.sleep(self.settle_seconds)
            compose_after = self._copy_input_text()
            if compose_still_contains_submitted_text(text, compose_after):
                retry_reason = "message_still_in_compose_after_first_submit"
                if visual_before is None:
                    visual_before = self._grab_window_image()
                retry_used = self._submit_input_alternate(submit_used)
                submit_attempts.append(retry_used)
                time.sleep(self.settle_seconds)
                compose_after = self._copy_input_text()
                if compose_still_contains_submitted_text(text, compose_after):
                    raise BridgeError("message still in compose box after retry submit; not acking")
            visual_delta: dict[str, Any] = {}
            coordinate_submit_used = any("coordinate_fallback" in attempt for attempt in submit_attempts)
            if visual_before is not None and coordinate_submit_used:
                visual_delta = self._verify_visible_outgoing_bubble(visual_before, evidence_id=evidence_id)
                if not visual_delta.get("verified"):
                    self._raise_if_login_required()
                    raise BridgeError("coordinate fallback did not create a visible outgoing bubble; not acking")
            target_after = self._target_chat_evidence(chat)
            return {
                "verified": True,
                "automation": "uia_wechat4",
                "chat": chat,
                "target_evidence": {
                    "expected": chat,
                    "before_send": target_before,
                    "after_send": target_after,
                    "matched": bool(target_before.get("matched") or target_after.get("matched")),
                    "mismatch": bool(target_before.get("mismatch") or target_after.get("mismatch")),
                },
                "submit_method": self.submit_method,
                "submit_used": " -> ".join(submit_attempts),
                "submit_attempts": submit_attempts,
                "submit_attempt_count": len(submit_attempts),
                "retry_reason": retry_reason,
                "text_chars": len(text),
                "compose_before_chars": len(compose_before),
                "compose_after_chars": len(compose_after),
                "message_visible_verified": bool(visual_delta.get("verified")),
                "delivery_verified": "visible_outgoing_bubble" if visual_delta.get("verified") else "",
                "visual_delta": visual_delta,
                **self._execution_profile(),
            }

    def send_image(
        self,
        chat: str,
        image_path: str,
        *,
        preserve_animation: bool = False,
        evidence_id: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            path = Path(image_path)
            if not path.is_file():
                raise BridgeError(f"media file not found: {path}")
            self.activate()
            self._raise_if_login_required()
            target_before = self._ensure_target_chat_ready(chat, "before_send")
            if not self._locate_input():
                raise BridgeError("chat input was not found by UIA")

            needs_visual_verification = self._use_coord_fallback or self._input_control is None
            self._clear_input_for_media()
            paste_before = self._grab_window_image() if needs_visual_verification else None

            clipboard_method = copy_media_to_clipboard(path, preserve_animation=preserve_animation)
            time.sleep(0.2)
            self._paste_clipboard_into_input()
            time.sleep(0.6)
            preview_delta: dict[str, Any] = {}
            if needs_visual_verification:
                preview_delta = self._verify_visible_media_preview(paste_before, evidence_id=evidence_id)
                if not preview_delta.get("verified"):
                    self._raise_if_login_required()
                    raise BridgeError("clipboard paste did not create a visible media preview; not sending")
            visual_before = self._grab_window_image() if needs_visual_verification else None
            submit_used = self._submit_input()
            time.sleep(self.settle_seconds)
            visual_delta: dict[str, Any] = {}
            if needs_visual_verification:
                visual_delta = self._verify_visible_outgoing_media(visual_before, evidence_id=evidence_id)
                if not visual_delta.get("verified"):
                    self._raise_if_login_required()
                    raise BridgeError("coordinate fallback did not create a visible outgoing media bubble; not acking")
            target_after = self._target_chat_evidence(chat)
            return {
                "verified": True if needs_visual_verification else "uia_paste_submit",
                "automation": "uia_wechat4",
                "chat": chat,
                "target_evidence": {
                    "expected": chat,
                    "before_send": target_before,
                    "after_send": target_after,
                    "matched": bool(target_before.get("matched") or target_after.get("matched")),
                    "mismatch": bool(target_before.get("mismatch") or target_after.get("mismatch")),
                },
                "path": str(path),
                "preserve_animation": bool(preserve_animation),
                "submit_method": self.submit_method,
                "submit_used": submit_used,
                "media_preview_verified": bool(preview_delta.get("verified")),
                "preview_delta": preview_delta,
                "media_visible_verified": bool(visual_delta.get("verified")),
                "media_delivery_status": visual_delta.get("delivery_status", {}),
                "delivery_verified": "visible_outgoing_media" if visual_delta.get("verified") else "",
                "visual_delta": visual_delta,
                "clipboard_method": clipboard_method,
                **self._execution_profile(),
            }


def log_event(kind: str, **fields: Any) -> None:
    payload = {"event": kind, "time": time.time(), **fields}
    print_json(payload)


def is_allowed_chat(chat: str, allowed_chats: list[str], allow_any_chat: bool) -> bool:
    return allow_any_chat or chat_is_allowed(chat, allowed_chats)


def normalize_compose_text(value: str) -> str:
    return "\n".join(line.rstrip() for line in str(value).replace("\r\n", "\n").replace("\r", "\n").strip().split("\n")).strip()


def compose_still_contains_submitted_text(expected: str, actual: str) -> bool:
    expected_norm = normalize_compose_text(expected)
    actual_norm = normalize_compose_text(actual)
    if not expected_norm or not actual_norm:
        return False
    return (
        actual_norm == expected_norm
        or expected_norm in actual_norm
        or actual_norm in expected_norm
    )


def voice_record_seconds(value: Any = None) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        seconds = VOICE_DEFAULT_RECORD_SECONDS
    if seconds <= 0:
        seconds = VOICE_DEFAULT_RECORD_SECONDS
    return max(VOICE_MIN_RECORD_SECONDS, min(VOICE_MAX_RECORD_SECONDS, seconds))


def voice_record_seconds_for_audio(
    requested_seconds: Any = None,
    *,
    audio_duration_seconds: float = 0.0,
) -> float:
    try:
        audio_seconds = float(audio_duration_seconds)
    except (TypeError, ValueError):
        audio_seconds = 0.0
    if audio_seconds > 0:
        return voice_record_seconds(audio_seconds + VOICE_TTS_RECORD_PADDING_SECONDS)
    return voice_record_seconds(requested_seconds)


def compact_native_voice_route_session(session: dict[str, Any] | None) -> dict[str, Any]:
    if not session:
        return {}
    set_capture = dict(session.get("set_default_capture") or {})
    set_render = dict(session.get("set_default_render") or {})
    return {
        "ok": bool(session.get("ok")),
        "attempted": bool(session.get("attempted")),
        "error": str(session.get("error") or ""),
        "candidate_kind": str((session.get("candidate") or {}).get("kind") or ""),
        "selected_capture_endpoint": session.get("selected_capture_endpoint") or {},
        "selected_render_endpoint": session.get("selected_render_endpoint") or {},
        "selected_capture_device_index": session.get("selected_capture_device_index"),
        "selected_render_device_index": session.get("selected_render_device_index"),
        "set_default_capture": {
            "ok": bool(set_capture.get("ok")),
            "verified": bool(set_capture.get("verified")),
            "error": str(set_capture.get("error") or ""),
            "target_endpoint": set_capture.get("target_endpoint") or {},
            "roles": set_capture.get("roles") or [],
            "set_results": set_capture.get("set_results") or [],
        },
        "set_default_render": {
            "ok": bool(set_render.get("ok")) if set_render else False,
            "verified": bool(set_render.get("verified")) if set_render else False,
            "error": str(set_render.get("error") or ""),
            "target_endpoint": set_render.get("target_endpoint") or {},
            "roles": set_render.get("roles") or [],
            "set_results": set_render.get("set_results") or [],
        },
    }


def compact_native_voice_route_restore(restore_result: dict[str, Any] | None) -> dict[str, Any]:
    if not restore_result:
        return {}
    return {
        "ok": bool(restore_result.get("ok")),
        "restored": bool(restore_result.get("restored")),
        "verified": bool(restore_result.get("verified")),
        "capture_verified": bool(restore_result.get("capture_verified")),
        "render_verified": bool(restore_result.get("render_verified")),
        "error": str(restore_result.get("error") or ""),
        "roles": restore_result.get("roles") or [],
        "capture_roles": restore_result.get("capture_roles") or [],
        "render_roles": restore_result.get("render_roles") or [],
    }


def audio_file_duration_seconds(path: str | Path | None) -> float:
    if not path:
        return 0.0
    audio_path = Path(path)
    if audio_path.suffix.lower() != ".wav" or not audio_path.exists():
        return 0.0
    try:
        with wave.open(str(audio_path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
            if frames > 0 and rate > 0:
                return frames / float(rate)
    except Exception:
        return 0.0
    return 0.0


def prepare_native_voice_audio_playback(
    path: str | Path | None,
    *,
    route_session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Do the slow decode/resample + resolve the output device BEFORE the WeChat
    recording starts, returning a handle whose actual playback (play_prepared_*) is
    near-instant. This keeps the leading silence in the native voice bubble minimal."""
    raw_path = str(path or "").strip()
    if not raw_path:
        return {"mode": "none", "started": False, "error": "audio path is empty"}
    audio_path = Path(raw_path)
    if not audio_path.exists():
        return {"mode": "none", "started": False, "path": str(audio_path), "error": "audio file does not exist"}
    if os.name != "nt":
        return {"mode": "none", "started": False, "path": str(audio_path), "error": "windows audio playback is required"}
    if audio_path.suffix.lower() != ".wav":
        return {"mode": "none", "started": False, "path": str(audio_path), "error": "native voice TTS playback supports wav only"}
    file_info = audio_file_info(audio_path)
    if not file_info.get("valid"):
        return {"mode": "none", "started": False, "path": str(audio_path), "file": file_info,
                "error": file_info.get("error") or "audio file is not playable"}

    # 默认走 winsound(系统默认共享输出)——微信录音的立体声混音采的就是这路共享混音，最稳，
    # 且让 Windows 共享混音器处理重采样(绕开 16k→设备率 的疑点)。
    # 想用低延迟的指定 PortAudio 设备播放,设 WECHAT_PC_VOICE_PLAYBACK=portaudio(注意该独占/WDM-KS 路可能不被立体声混音采到→语音没声音)。
    playback_mode = os.environ.get("WECHAT_PC_VOICE_PLAYBACK", "").strip().lower()
    if not playback_mode:
        # 自动:虚拟声卡(VB-CABLE)→portaudio 专播到 cable 输入端(只送 TTS,系统声不混入);
        # 其它(如立体声混音监听默认输出)→winsound 播默认共享输出。
        cand_kind = str(((route_session or {}).get("candidate") or {}).get("kind") or "")
        playback_mode = "portaudio" if cand_kind == "virtual_audio_cable" else "winsound"
    selected_render = dict((route_session or {}).get("selected_render_endpoint") or {})
    device_index = -1
    if playback_mode == "portaudio":
        raw_index = (route_session or {}).get("selected_render_device_index")
        if raw_index not in (None, ""):
            try:
                device_index = int(raw_index)
            except (TypeError, ValueError):
                device_index = -1
        if device_index < 0 and selected_render:
            selection = select_portaudio_device("render", selected_render)
            if selection.get("ok"):
                try:
                    device_index = int(selection.get("device_index"))
                except (TypeError, ValueError):
                    device_index = -1
    if device_index >= 0:
        prepared = prepare_wav_for_portaudio_device(
            audio_path,
            device_index,
            selected={"ok": True, "kind": "render", "endpoint": selected_render, "device_index": device_index},
        )
        if prepared.get("ok"):
            prewarm = prewarm_portaudio_device(prepared)
            return {"mode": "portaudio", "prepared": prepared, "prewarm": prewarm, "file": file_info, "path": str(audio_path)}
        return {"mode": "none", "started": False, "path": str(audio_path), "file": file_info,
                "error": prepared.get("error") or "playback preparation failed", "prepare": prepared}
    # No PortAudio device resolvable: fall back to winsound (no pre-resample needed).
    return {"mode": "winsound", "path": str(audio_path), "file": file_info}


def play_prepared_native_voice_audio_playback(prepared: dict[str, Any] | None) -> dict[str, Any]:
    """Fast playback trigger for a handle from prepare_native_voice_audio_playback."""
    if not isinstance(prepared, dict):
        return {"started": False, "error": "no prepared playback handle"}
    mode = prepared.get("mode")
    if mode == "portaudio":
        result = play_prepared_portaudio(prepared["prepared"])
        result["file"] = prepared.get("file")
        result["prewarm"] = prepared.get("prewarm")
        return result
    if mode == "winsound":
        try:
            import winsound  # type: ignore

            winsound.PlaySound(str(prepared["path"]), winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as exc:
            return {"started": False, "path": prepared.get("path"), "error": str(exc)}
        return {
            "started": True,
            "path": prepared.get("path"),
            "method": "winsound_async_default_output",
            "duration_seconds": round(audio_file_duration_seconds(Path(prepared["path"])), 3),
            "file": prepared.get("file"),
        }
    return {"started": False, "error": prepared.get("error") or "playback was not prepared"}


def start_native_voice_audio_playback(
    path: str | Path | None,
    *,
    route_session: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_path = str(path or "").strip()
    if not raw_path:
        return {"started": False, "error": "audio path is empty"}
    audio_path = Path(raw_path)
    if not audio_path.exists():
        return {"started": False, "path": str(audio_path), "error": "audio file does not exist"}
    if os.name != "nt":
        return {"started": False, "path": str(audio_path), "error": "windows audio playback is required"}
    if audio_path.suffix.lower() != ".wav":
        return {
            "started": False,
            "path": str(audio_path),
            "error": "native voice TTS playback currently supports wav audio only",
        }
    file_info = audio_file_info(audio_path)
    if not file_info.get("valid"):
        return {
            "started": False,
            "path": str(audio_path),
            "file": file_info,
            "error": file_info.get("error") or "audio file is not playable",
        }
    selected_render = dict((route_session or {}).get("selected_render_endpoint") or {})
    selected_render_device_index = (route_session or {}).get("selected_render_device_index")
    if selected_render_device_index not in (None, ""):
        try:
            device_index = int(selected_render_device_index)
        except (TypeError, ValueError):
            device_index = -1
        if device_index >= 0:
            direct_playback = play_wav_to_portaudio_device(
                audio_path,
                device_index,
                selected={
                    "ok": True,
                    "kind": "render",
                    "endpoint": selected_render,
                    "device_index": device_index,
                    "selection_source": "route_session_proven_loopback_device_index",
                },
            )
            if direct_playback.get("started"):
                direct_playback["file"] = file_info
                return direct_playback
            return {
                "started": False,
                "path": str(audio_path),
                "method": "sounddevice_portaudio_index",
                "selected_render_endpoint": selected_render,
                "selected_render_device_index": device_index,
                "file": file_info,
                "error": direct_playback.get("error") or "direct PortAudio playback failed",
                "portaudio_playback": direct_playback,
            }
    if selected_render:
        endpoint_playback = play_wav_to_render_endpoint(audio_path, selected_render)
        if endpoint_playback.get("started"):
            endpoint_playback["file"] = file_info
            return endpoint_playback
        return {
            "started": False,
            "path": str(audio_path),
            "method": "sounddevice_endpoint",
            "selected_render_endpoint": selected_render,
            "file": file_info,
            "error": endpoint_playback.get("error") or "direct endpoint playback failed",
            "endpoint_playback": endpoint_playback,
        }
    try:
        import winsound  # type: ignore

        winsound.PlaySound(str(audio_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception as exc:
        return {"started": False, "path": str(audio_path), "error": str(exc)}
    return {
        "started": True,
        "path": str(audio_path),
        "method": "winsound_async_default_output",
        "duration_seconds": round(audio_file_duration_seconds(audio_path), 3),
        "file": file_info,
    }


def stop_native_voice_audio_playback() -> dict[str, Any]:
    if os.name != "nt":
        return {"stopped": False, "error": "windows audio playback is required"}
    sounddevice_stop = stop_sounddevice_playback()
    winsound_stop: dict[str, Any] = {}
    try:
        import winsound  # type: ignore

        winsound.PlaySound(None, 0)
        winsound_stop = {"stopped": True, "method": "winsound_stop"}
    except Exception as exc:
        winsound_stop = {"stopped": False, "method": "winsound_stop", "error": str(exc)}
    return {
        "stopped": bool(sounddevice_stop.get("stopped") or winsound_stop.get("stopped")),
        "method": "sounddevice_stop+winsound_stop",
        "sounddevice": sounddevice_stop,
        "winsound": winsound_stop,
    }


def real_delivery_evidence_error(evidence: Any) -> str:
    if not isinstance(evidence, dict):
        return "controller did not return structured delivery evidence; not acking"
    target_error = target_evidence_error(evidence.get("target_evidence"))
    if target_error:
        return target_error
    media_status = evidence.get("media_delivery_status")
    if not isinstance(media_status, dict):
        visual_delta = evidence.get("visual_delta")
        if isinstance(visual_delta, dict):
            media_status = visual_delta.get("delivery_status")
    if isinstance(media_status, dict):
        state = str(media_status.get("state") or "")
        if state == "pending":
            return "outgoing media is still pending in WeChat; not acking"
        if state == "failed":
            return "outgoing media shows a WeChat failure marker; not acking"
    if (
        evidence.get("bubble_verified") is True
        or evidence.get("message_visible_verified") is True
        or evidence.get("media_visible_verified") is True
    ):
        return ""
    if str(evidence.get("delivery_verified") or "") in {"visible_outgoing_bubble", "visible_outgoing_media"}:
        return ""
    if (
        evidence.get("uses_coord_fallback") is True
        or str(evidence.get("input_mode") or "") == "coordinate_fallback"
        or str(evidence.get("submit_used") or "") == "enter_coordinate_fallback"
    ):
        return "coordinate fallback cannot verify an actual outgoing bubble; not acking"
    if not evidence.get("verified"):
        return "controller did not provide positive delivery evidence; not acking"
    return ""


def capture_failure(controller: Any, item_id: str) -> str:
    if controller is None or not hasattr(controller, "capture"):
        return ""
    raw_dir = os.environ.get("WECHAT_PC_BRIDGE_CAPTURE_DIR", "").strip()
    if raw_dir:
        capture_dir = Path(raw_dir)
    else:
        capture_dir = Path(__file__).resolve().parents[2] / "runtime" / "data" / "pc-weixin-captures"
    safe_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in item_id)[:80] or "unknown"
    path = capture_dir / f"send-failed-{safe_id}.png"
    try:
        return str(controller.capture(path))
    except Exception as exc:
        return f"capture_failed: {exc}"


def report_delivery_safe(
    api: AdapterApi,
    item_id: str,
    *,
    status: str,
    delivered: bool,
    dry_run: bool,
    ack: bool,
    metadata: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    try:
        result = api.delivery(
            item_id,
            status=status,
            delivered=delivered,
            dry_run=dry_run,
            ack=ack,
            metadata=metadata,
            error=error,
        )
        log_event(
            "delivery_reported",
            id=item_id,
            status=status,
            delivered=delivered,
            dry_run=dry_run,
            ack_requested=ack,
            acked=bool(result.get("acked")),
        )
        return result
    except Exception as exc:
        log_event("delivery_report_failed", id=item_id, status=status, error=str(exc))
        return {"ok": False, "acked": False, "error": str(exc)}


def build_controller(args: argparse.Namespace) -> Any:
    if args.automation == "win32":
        return Win32WeixinController(
            search_mode=args.search_mode,
            submit_method=args.submit_method,
            settle_seconds=args.settle_seconds,
        )
    return UiaWeixinController(
        search_enabled=args.search_mode != "none",
        submit_method=args.submit_method,
        settle_seconds=args.settle_seconds,
        assume_target_confirmed=getattr(args, "assume_target_confirmed", False),
    )


def pc_weixin_capabilities(*, dry_run: bool, allow_voice_file_fallback: bool) -> dict[str, str]:
    text_out = "ready-live-proven-dry-run-active" if dry_run else "ready-live-proven-via-uia"
    image_out = "ready-live-proven-dry-run-active" if dry_run else "ready-live-proven-via-uia-clipboard"
    sticker_out = "ready-live-proven-gif-dry-run-active" if dry_run else "ready-live-proven-via-uia-file-drop-gif"
    voice_out = "ready-native-bubble-uia-content-route-not-proven-dry-run-active" if dry_run else "ready-native-bubble-uia-content-route-not-proven"
    if allow_voice_file_fallback:
        voice_out += "+file-fallback-available"
    return {
        "queue_poll": "ready",
        "queue_ack": "ready",
        "text_in": "via-weflow-bridge",
        "text_out": text_out,
        "image_out": image_out,
        "voice_out": voice_out,
        "animated_sticker_out": sticker_out,
        "realtime_voice": "unsupported",
    }


def blocked_pc_weixin_capabilities(reason: str, *, allow_voice_file_fallback: bool) -> dict[str, str]:
    capabilities = pc_weixin_capabilities(
        dry_run=False,
        allow_voice_file_fallback=allow_voice_file_fallback,
    )
    capabilities["text_out"] = reason
    capabilities["image_out"] = reason
    capabilities["animated_sticker_out"] = reason
    capabilities["voice_out"] = reason
    return capabilities


def controller_probe_ready(probe: dict[str, Any] | None) -> bool:
    if not isinstance(probe, dict):
        return False
    if probe.get("login_required") is True:
        return False
    capabilities = probe.get("capabilities")
    if isinstance(capabilities, dict):
        text_out = str(capabilities.get("text_out") or "")
        if text_out.startswith("blocked"):
            return False
    if "input_located" in probe:
        return bool(probe.get("input_located"))
    return True


def controller_blocker_from_state(
    *,
    probe: dict[str, Any] | None = None,
    error: str = "",
) -> tuple[str, str, str]:
    if isinstance(probe, dict) and probe.get("login_required") is True:
        return (
            "blocked-login-required",
            "blocked_login_required",
            "PC WeChat is showing a login/security window; pending items will retry after the main chat window returns.",
        )
    if error:
        if is_login_required_error_text(error):
            return (
                "blocked-login-required",
                "blocked_login_required",
                "PC WeChat is showing a login/security window; pending items will retry after the main chat window returns.",
            )
        return ("blocked-window-not-ready", "blocked_window_not_ready", error)
    if isinstance(probe, dict) and not controller_probe_ready(probe):
        locate_error = str(probe.get("locate_error") or "")
        return (
            "blocked-input-not-found",
            "blocked_input_not_found",
            locate_error or "PC WeChat input box is not ready.",
        )
    return ("online-live-executor", "", "")


def heartbeat_payload(
    args: argparse.Namespace,
    *,
    dry_run: bool,
    controller_probe: dict[str, Any] | None = None,
    controller_error: str = "",
) -> dict[str, Any]:
    allow_voice_file_fallback = bool(getattr(args, "allow_voice_file_fallback", False))
    allowed_chats = list(getattr(args, "allowed_chat", []) or [])
    allow_any_chat = bool(getattr(args, "allow_any_chat", False))
    status = "online-dry-run" if dry_run else "online-live-executor"
    capabilities = pc_weixin_capabilities(
        dry_run=dry_run,
        allow_voice_file_fallback=allow_voice_file_fallback,
    )
    blocker_code = ""
    blocker_message = ""
    if not dry_run:
        status, blocker_code, blocker_message = controller_blocker_from_state(
            probe=controller_probe,
            error=controller_error,
        )
        if blocker_code:
            capabilities = blocked_pc_weixin_capabilities(
                blocker_code,
                allow_voice_file_fallback=allow_voice_file_fallback,
            )
    return {
        "type": "pc_weixin",
        "platform": "windows_wechat",
        "driver": f"{getattr(args, 'automation', 'uia')}_wechat4",
        "account_id": str(getattr(args, "account_id", "") or "wechat-main"),
        "device_id": str(os.environ.get("COMPUTERNAME") or ""),
        "status": status,
        "ttl_seconds": float(getattr(args, "heartbeat_ttl", 90.0) or 90.0),
        "capabilities": capabilities,
        "runtime": {
            "bridge": "pc_weixin_bridge",
            "mode": "dry_run" if dry_run else "live",
            "automation": str(getattr(args, "automation", "uia")),
            "ack_dry_run": bool(getattr(args, "ack_dry_run", False)),
            "allow_voice_file_fallback": allow_voice_file_fallback,
            "allow_any_chat": allow_any_chat,
            "allowed_chat_count": len(allowed_chats),
            "whitelist_mode": "allow_any" if allow_any_chat else "stable_id_list",
            "instance_root": str(getattr(args, "instance_root", "") or ""),
            "window_ready": bool(dry_run or (controller_probe_ready(controller_probe) and not controller_error)),
            "blocker_code": blocker_code,
            "blocker_message": blocker_message,
            "controller_error": controller_error,
            "window_title": str((controller_probe or {}).get("title") or "") if isinstance(controller_probe, dict) else "",
            "window_class": str((controller_probe or {}).get("class") or "") if isinstance(controller_probe, dict) else "",
            "window_size": list((controller_probe or {}).get("size") or []) if isinstance(controller_probe, dict) else [],
            "window_login_required": bool((controller_probe or {}).get("login_required")) if isinstance(controller_probe, dict) else False,
            "window_scan": ((controller_probe or {}).get("window_scan") or {}) if isinstance(controller_probe, dict) else {},
        },
    }


def handle_item(
    item: QueueItem,
    *,
    api: AdapterApi,
    controller: Any | None,
    dry_run: bool,
    ack_dry_run: bool,
    allowed_chats: list[str],
    allow_any_chat: bool,
    allow_voice_file_fallback: bool = False,
    allow_stable_id_name_search: bool = False,
    auto_switch_target: bool = True,
) -> bool:
    action = item.action
    action_type = str(action.get("type") or "text")
    chat = str(action.get("chat_id") or "")
    item_metadata = item.metadata if isinstance(item.metadata, dict) else {}
    action_metadata = action.get("metadata") if isinstance(action.get("metadata"), dict) else {}
    metadata = {**item_metadata, **action_metadata}
    force_dry_run = metadata_flag(item_metadata, "force_dry_run") or metadata_flag(item_metadata, "dry_run")
    force_dry_run = force_dry_run or metadata_flag(action_metadata, "force_dry_run") or metadata_flag(action_metadata, "dry_run")
    effective_dry_run = dry_run or force_dry_run
    configured_target_display_name = str(metadata.get("target_display_name") or "").strip()
    target_display_name = configured_target_display_name or chat
    stable_chat_id = looks_like_stable_chat_id(chat)
    generic_name_search_requested = metadata_flag(metadata, "allow_name_search")
    # Stable wxid sends are only allowed to use display-name search through the
    # explicit stable-id escape hatch; generic name search is too easy to set
    # from higher layers and can open the wrong chat.
    stable_id_name_search_allowed = bool(
        allow_stable_id_name_search
        or metadata_flag(metadata, "allow_stable_id_name_search")
    )
    stable_id_current_chat_only = stable_chat_id and not stable_id_name_search_allowed
    text = str(action.get("text") or "")
    voice_text = str(action.get("voice_text") or action.get("text") or "")
    media_path = str(action.get("path") or "").strip()

    native_voice_supported = bool(
        action_type == "voice"
        and (
            effective_dry_run
            or (
                controller is not None
                and hasattr(controller, "send_voice")
                and bool(getattr(controller, "native_voice_supported", True))
            )
        )
    )
    voice_file_fallback = action_type == "voice" and not native_voice_supported and allow_voice_file_fallback and bool(media_path)
    if action_type == "voice" and not native_voice_supported and not voice_file_fallback:
        log_event(
            "unsupported_action",
            id=item.id,
            action_type=action_type,
            chat_id=chat,
            reason="pc_weixin_uia_native_voice_bubble_unavailable",
        )
        return False
    if action_type not in {"text", *MEDIA_ACTION_TYPES, "voice"}:
        log_event("unsupported_action", id=item.id, action_type=action_type, chat_id=chat)
        return False
    if action_type == "text" and not text.strip():
        log_event("empty_text_skipped", id=item.id, chat_id=chat)
        return False
    if action_type in MEDIA_ACTION_TYPES and not media_path:
        log_event("missing_media_path", id=item.id, action_type=action_type, chat_id=chat)
        return False
    if not is_allowed_chat(chat, allowed_chats, allow_any_chat):
        log_event("chat_blocked", id=item.id, chat_id=chat, allowed_chats=normalize_allowed_chats(allowed_chats))
        return False
    if not effective_dry_run and stable_chat_id and not configured_target_display_name:
        log_event(
            "target_display_name_required",
            id=item.id,
            chat_id=chat,
            reason="visual PC Weixin bridge needs a display name to open the chat, while whitelist still uses stable id",
        )
        raise BridgeError("target_display_name is required for real PC Weixin send when chat_id is a stable id")

    if effective_dry_run:
        evidence = {
            "executor": "pc_weixin_bridge",
            "action_type": action_type,
            "chat_id": chat,
            "target_display_name": target_display_name,
            "process_dry_run": dry_run,
            "force_dry_run": force_dry_run,
            "stable_id_current_chat_only": stable_id_current_chat_only,
            "allow_stable_id_name_search": stable_id_name_search_allowed,
            "generic_allow_name_search_ignored": bool(stable_chat_id and generic_name_search_requested),
            "text_chars": len(text),
            "voice_text_chars": len(voice_text),
            "path": media_path,
            "native_voice_supported": native_voice_supported,
            "voice_file_fallback": voice_file_fallback,
        }
        if dry_run and not force_dry_run:
            log_event(
                "blocked_live_item_in_dry_run",
                id=item.id,
                action_type=action_type,
                chat_id=chat,
                target_display_name=target_display_name,
                text=text,
                path=media_path,
            )
            report_delivery_safe(
                api,
                item.id,
                status="blocked_live_item_in_dry_run",
                delivered=False,
                dry_run=True,
                ack=False,
                metadata=evidence,
                error="dry-run bridge refused to ack an unmarked live queue item",
            )
            return False
        log_event(
            "forced_dry_run_send" if force_dry_run and not dry_run else "dry_run_send",
            id=item.id,
            action_type=action_type,
            chat_id=chat,
            target_display_name=target_display_name,
            process_dry_run=dry_run,
            force_dry_run=force_dry_run,
            text=text,
            path=media_path,
            voice_file_fallback=voice_file_fallback,
        )
        delivery = report_delivery_safe(
            api,
            item.id,
            status="dry_run_verified",
            delivered=True,
            dry_run=True,
            ack=ack_dry_run,
            metadata=evidence,
        )
        if ack_dry_run and delivery.get("acked"):
            log_event("acked_dry_run", id=item.id)
        return True

    if controller is None:
        raise BridgeError("controller is required for real sending")
    attempted_evidence: dict[str, Any] = {}
    send_policy = {
        "stable_chat_id": stable_chat_id,
        "stable_id_current_chat_only": stable_id_current_chat_only,
        "allow_stable_id_name_search": stable_id_name_search_allowed,
        "generic_allow_name_search_ignored": bool(stable_chat_id and generic_name_search_requested),
        "visible_conversation_switch_allowed": (not stable_id_current_chat_only) or auto_switch_target,
        "auto_switch_target": auto_switch_target,
    }
    previous_search_enabled: bool | None = None
    previous_visible_conversation_switch_enabled: bool | None = None
    try:
        if stable_id_current_chat_only and hasattr(controller, "search_enabled"):
            try:
                previous_search_enabled = bool(getattr(controller, "search_enabled"))
                if previous_search_enabled:
                    setattr(controller, "search_enabled", False)
                    log_event(
                        "stable_id_name_search_disabled",
                        id=item.id,
                        chat_id=chat,
                        target_display_name=target_display_name,
                    )
            except Exception as exc:
                log_event(
                    "stable_id_name_search_guard_failed",
                    id=item.id,
                    chat_id=chat,
                    target_display_name=target_display_name,
                    error=str(exc),
                )
        # auto_switch_target 开启时：保留"点会话列表"跳转能力(按目标显示名精确点击)，
        # 跳转后 _ensure_target_chat_ready 仍会核实，mismatch 直接 raise、绝不发到错的会话。
        # 关闭时沿用旧的"仅当前窗口"严格行为(非目标会话即报错)。
        if stable_id_current_chat_only and not auto_switch_target and hasattr(controller, "visible_conversation_switch_enabled"):
            try:
                previous_visible_conversation_switch_enabled = bool(
                    getattr(controller, "visible_conversation_switch_enabled")
                )
                if previous_visible_conversation_switch_enabled:
                    setattr(controller, "visible_conversation_switch_enabled", False)
                    log_event(
                        "stable_id_visible_conversation_switch_disabled",
                        id=item.id,
                        chat_id=chat,
                        target_display_name=target_display_name,
                    )
            except Exception as exc:
                log_event(
                    "stable_id_visible_conversation_switch_guard_failed",
                    id=item.id,
                    chat_id=chat,
                    target_display_name=target_display_name,
                    error=str(exc),
                )
        if action_type == "text":
            evidence = controller.send_text(target_display_name, text, evidence_id=item.id) or {}
        elif action_type == "voice" and native_voice_supported:
            duration_raw = (
                action.get("duration_seconds")
                or action.get("duration")
                or metadata.get("duration_seconds")
                or metadata.get("voice_duration_seconds")
            )
            evidence = controller.send_voice(
                target_display_name,
                voice_text=voice_text,
                audio_path=media_path,
                duration_seconds=voice_record_seconds(duration_raw),
                evidence_id=item.id,
            ) or {}
        else:
            if voice_file_fallback:
                log_event(
                    "voice_file_fallback_send",
                    id=item.id,
                    chat_id=chat,
                    target_display_name=target_display_name,
                    path=media_path,
                )
            evidence = controller.send_image(
                target_display_name,
                media_path,
                preserve_animation=action_type == "animated_sticker",
                evidence_id=item.id,
            ) or {}
            if voice_file_fallback:
                evidence = {**evidence, "voice_delivery": "file_fallback"}
        if isinstance(evidence, dict):
            attempted_evidence = evidence
        evidence_error = real_delivery_evidence_error(evidence)
        if evidence_error:
            raise BridgeError(evidence_error)
    except Exception as exc:
        error_text = str(exc)
        login_blocked = is_login_required_error_text(error_text)
        capture_path = "" if login_blocked else capture_failure(controller, item.id)
        evidence = {
            "executor": "pc_weixin_bridge",
            "action_type": action_type,
            "chat_id": chat,
            "target_display_name": target_display_name,
            "capture": capture_path,
        }
        target_evidence = getattr(controller, "_last_target_evidence", None)
        if isinstance(target_evidence, dict) and target_evidence:
            evidence["target_evidence"] = target_evidence
        if attempted_evidence:
            evidence["evidence"] = attempted_evidence
        evidence["send_policy"] = send_policy
        status = "blocked_login_required_not_acked" if login_blocked else "send_failed_not_acked"
        log_event(
            status,
            id=item.id,
            chat_id=chat,
            action_type=action_type,
            target_display_name=target_display_name,
            error=error_text,
            capture=capture_path,
        )
        report_delivery_safe(
            api,
            item.id,
            status=status,
            delivered=False,
            dry_run=False,
            ack=False,
            metadata=evidence,
            error=error_text,
        )
        raise
    finally:
        if previous_search_enabled is not None:
            try:
                setattr(controller, "search_enabled", previous_search_enabled)
            except Exception:
                pass
        if previous_visible_conversation_switch_enabled is not None:
            try:
                setattr(controller, "visible_conversation_switch_enabled", previous_visible_conversation_switch_enabled)
            except Exception:
                pass
    log_event(
        "send_verified",
        id=item.id,
        action_type=action_type,
        chat_id=chat,
        target_display_name=target_display_name,
        evidence=evidence,
    )
    delivery = report_delivery_safe(
        api,
        item.id,
        status="sent_verified",
        delivered=True,
        dry_run=False,
        ack=True,
        metadata={
            "executor": "pc_weixin_bridge",
            "action_type": action_type,
            "chat_id": chat,
            "target_display_name": target_display_name,
            "send_policy": send_policy,
            "evidence": evidence,
        },
    )
    log_event(
        "sent_verified_and_acked",
        id=item.id,
        chat_id=chat,
        target_display_name=target_display_name,
        acked=bool(delivery.get("acked")),
    )
    return True


def metadata_flag(metadata: dict[str, Any], key: str) -> bool:
    value = metadata.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def run_poll(args: argparse.Namespace, *, once: bool) -> int:
    secret = args.secret or os.environ.get(args.secret_env, "")
    api = AdapterApi(args.base_url, args.connector_id, secret=secret)
    dry_run = not args.allow_real_send
    controller: Any | None = None
    controller_probe: dict[str, Any] | None = None
    controller_error = ""
    controller_ready = bool(dry_run)
    last_probe = 0.0
    last_heartbeat = 0.0
    # 同一条 item 跨 poll 周期的发送尝试次数。发送后核实失败(如"still in compose box"
    # 误判)会不 ack→留在 pending→下次 poll 又是同一条→重复真发(曾出现 1 条被发 8 次)。
    # 这里按 item.id 计数，超过上限就强制 ack 止损，把重复封顶在 max_send_attempts。
    send_attempts: dict[str, int] = {}
    max_send_attempts = max(1, int(getattr(args, "max_send_attempts", 2) or 2))

    def refresh_controller_state(*, force: bool = False) -> bool:
        nonlocal controller, controller_probe, controller_error, controller_ready, last_probe
        if dry_run:
            controller_ready = True
            controller_probe = None
            controller_error = ""
            return True
        now = time.time()
        retry_interval = min(5.0, max(1.0, float(args.heartbeat_interval)))
        if not force and now - last_probe < retry_interval:
            return controller_ready
        last_probe = now
        try:
            if controller is None:
                controller = build_controller(args)
                log_event("controller_created", connector_id=args.connector_id, automation=args.automation)
            probe = controller.probe()
            controller_probe = probe if isinstance(probe, dict) else {}
            controller_error = ""
            controller_ready = controller_probe_ready(controller_probe)
            if controller_ready:
                log_event("controller_ready", connector_id=args.connector_id)
            else:
                status, blocker_code, blocker_message = controller_blocker_from_state(probe=controller_probe)
                log_event(
                    "controller_blocked",
                    connector_id=args.connector_id,
                    status=status,
                    blocker_code=blocker_code,
                    blocker_message=blocker_message,
                )
            return controller_ready
        except Exception as exc:
            controller_probe = None
            controller_error = str(exc)
            controller_ready = False
            controller = None
            status, blocker_code, blocker_message = controller_blocker_from_state(error=controller_error)
            log_event(
                "controller_unavailable",
                connector_id=args.connector_id,
                status=status,
                blocker_code=blocker_code,
                blocker_message=blocker_message,
            )
            return False

    while True:
        now = time.time()
        heartbeat_due = now - last_heartbeat >= max(1.0, float(args.heartbeat_interval))
        if not dry_run and (heartbeat_due or not controller_ready):
            refresh_controller_state(force=heartbeat_due)
        if heartbeat_due:
            try:
                state = api.heartbeat(
                    heartbeat_payload(
                        args,
                        dry_run=dry_run,
                        controller_probe=controller_probe,
                        controller_error=controller_error,
                    )
                )
                last_heartbeat = now
                log_event(
                    "heartbeat",
                    connector_id=args.connector_id,
                    online=(state.get("state") or {}).get("online"),
                    ready=bool(dry_run or controller_ready),
                    blocker_code=(state.get("state") or {}).get("runtime", {}).get("blocker_code", ""),
                )
            except Exception as exc:
                last_heartbeat = now
                log_event("heartbeat_failed", connector_id=args.connector_id, error=str(exc))
        if not dry_run and not controller_ready:
            status, blocker_code, blocker_message = controller_blocker_from_state(
                probe=controller_probe,
                error=controller_error,
            )
            log_event(
                "poll_paused",
                connector_id=args.connector_id,
                status=status,
                blocker_code=blocker_code,
                blocker_message=blocker_message,
            )
            if once:
                return 0
            time.sleep(args.poll_interval)
            continue
        items = api.poll(args.limit)
        log_event("poll", connector_id=args.connector_id, count=len(items), dry_run=dry_run)
        polled_ids = {item.id for item in items}
        # 已不在 pending 的 item 计数清掉(已发成功被 ack/或被取消),避免字典无限增长
        for stale_id in [k for k in send_attempts if k not in polled_ids]:
            send_attempts.pop(stale_id, None)
        for item in items:
            send_attempts[item.id] = send_attempts.get(item.id, 0) + 1
            attempt = send_attempts[item.id]
            try:
                handle_item(
                    item,
                    api=api,
                    controller=controller,
                    dry_run=dry_run,
                    ack_dry_run=args.ack_dry_run,
                allowed_chats=args.allowed_chat,
                allow_any_chat=args.allow_any_chat,
                allow_voice_file_fallback=args.allow_voice_file_fallback,
                allow_stable_id_name_search=args.allow_stable_id_name_search,
                auto_switch_target=getattr(args, "auto_switch_target", True),
            )
                send_attempts.pop(item.id, None)
            except Exception as exc:
                if not dry_run and is_login_required_error_text(str(exc)):
                    controller_ready = False
                log_event("item_error", id=item.id, error=str(exc), attempt=attempt, max_attempts=max_send_attempts)
                # 登录阻断不算入上限(等登录恢复后再发，且不会重复落地);其余失败到达上限即强制 ack 止损,
                # 防止核实误判导致同一条被反复真发。
                if not dry_run and attempt >= max_send_attempts and not is_login_required_error_text(str(exc)):
                    report_delivery_safe(
                        api,
                        item.id,
                        status="acked_after_max_send_attempts",
                        delivered=False,
                        dry_run=False,
                        ack=True,
                        metadata={"executor": "pc_weixin_bridge", "attempts": attempt, "max_attempts": max_send_attempts},
                        error=f"capped after {attempt} attempts to avoid duplicate sends: {exc}",
                    )
                    send_attempts.pop(item.id, None)
                    log_event("acked_after_max_send_attempts", id=item.id, attempts=attempt, max_attempts=max_send_attempts)
        if once:
            return 0
        time.sleep(args.poll_interval)


def run_probe(args: argparse.Namespace) -> int:
    controller = build_controller(args)
    print_json({"ok": True, "weixin": controller.probe()}, indent=2)
    return 0


def run_target_probe(args: argparse.Namespace) -> int:
    controller = build_controller(args)
    target = str(args.target or "").strip()
    if not target:
        raise BridgeError("--target is required")
    print_json({"ok": True, "target": controller.target_probe(target)}, indent=2)
    return 0


def run_capture(args: argparse.Namespace) -> int:
    controller = build_controller(args)
    path = controller.capture(Path(args.output))
    print_json({"ok": True, "path": str(path)}, indent=2)
    return 0


def run_send_test(args: argparse.Namespace) -> int:
    if not args.allow_real_send:
        log_event("dry_run_send_test", chat_id=args.to, text=args.text)
        return 0
    authorized_chat = str(args.chat_id or args.to)
    if not is_allowed_chat(authorized_chat, args.allowed_chat, args.allow_any_chat):
        raise BridgeError(f"chat is not allowed: {authorized_chat}")
    if looks_like_stable_chat_id(authorized_chat) and authorized_chat == args.to:
        raise BridgeError("--to must be the visible chat display name; pass the stable id with --chat-id")
    controller = build_controller(args)
    evidence = controller.send_text(args.to, args.text)
    log_event("sent_test", chat_id=authorized_chat, target_display_name=args.to, evidence=evidence)
    return 0


def run_send_voice_test(args: argparse.Namespace) -> int:
    duration = voice_record_seconds(args.duration_seconds)
    audio_path = str(getattr(args, "audio_path", "") or "").strip()
    if not args.allow_real_send:
        log_event("dry_run_send_voice_test", chat_id=args.to, text=args.text, duration_seconds=duration, audio_path=audio_path)
        return 0
    authorized_chat = str(args.chat_id or args.to)
    if not is_allowed_chat(authorized_chat, args.allowed_chat, args.allow_any_chat):
        raise BridgeError(f"chat is not allowed: {authorized_chat}")
    if looks_like_stable_chat_id(authorized_chat) and authorized_chat == args.to:
        raise BridgeError("--to must be the visible chat display name; pass the stable id with --chat-id")
    controller = build_controller(args)
    evidence = controller.send_voice(args.to, voice_text=args.text, audio_path=audio_path, duration_seconds=duration)
    log_event("sent_voice_test", chat_id=authorized_chat, target_display_name=args.to, evidence=evidence)
    return 0


def run_audio_route(args: argparse.Namespace) -> int:
    print_json(native_voice_route_status(getattr(args, "audio_path", "")), indent=2)
    return 0


def run_audio_loopback_diagnose(args: argparse.Namespace) -> int:
    result = diagnose_native_voice_loopback(
        getattr(args, "audio_path", ""),
        output_path=getattr(args, "output", "") or None,
    )
    print_json(result, indent=2)
    return 0 if result.get("ok") else 1


def run_audio_loopback_diagnose_all(args: argparse.Namespace) -> int:
    result = diagnose_all_native_voice_loopbacks(
        getattr(args, "audio_path", ""),
        output_dir=getattr(args, "output_dir", "") or None,
        max_attempts=int(getattr(args, "max_attempts", 12) or 12),
    )
    print_json(result, indent=2)
    return 0 if result.get("ok") else 1


def run_heartbeat(args: argparse.Namespace) -> int:
    secret = args.secret or os.environ.get(args.secret_env, "")
    dry_run = not args.allow_real_send
    api = AdapterApi(args.base_url, args.connector_id, secret=secret)
    controller_probe: dict[str, Any] | None = None
    controller_error = ""
    if not dry_run:
        try:
            controller = build_controller(args)
            probe = controller.probe()
            controller_probe = probe if isinstance(probe, dict) else {}
        except Exception as exc:
            controller_error = str(exc)
    state = api.heartbeat(
        heartbeat_payload(
            args,
            dry_run=dry_run,
            controller_probe=controller_probe,
            controller_error=controller_error,
        )
    )
    print_json({"ok": True, "connector_id": args.connector_id, "state": state.get("state")}, indent=2)
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PC Weixin 4.x visual bridge for the local adapter")
    parser.add_argument("--base-url", default="http://127.0.0.1:8898")
    parser.add_argument("--connector-id", default="pc_weixin")
    parser.add_argument("--instance-root", default="")
    parser.add_argument("--secret", default="")
    parser.add_argument("--secret-env", default="WECHAT_AGENT_SECRET")
    parser.add_argument("--allowed-chat", action="append", default=[])
    parser.add_argument("--allow-any-chat", action="store_true")
    parser.add_argument("--allow-real-send", action="store_true")
    parser.add_argument(
        "--max-send-attempts",
        type=int,
        default=2,
        help="同一条待发消息最多尝试真发几次；超过即强制 ack 止损，防止核实误判导致重复发送。",
    )
    parser.add_argument(
        "--auto-switch-target",
        dest="auto_switch_target",
        action="store_true",
        default=True,
        help="当前会话不是目标时自动跳转(点会话列表按显示名)，跳转后仍核实，不匹配绝不发。默认开。",
    )
    parser.add_argument(
        "--no-auto-switch-target",
        dest="auto_switch_target",
        action="store_false",
        help="关闭自动跳转，沿用旧的严格行为(非目标会话即报错、不发)。",
    )
    parser.add_argument(
        "--assume-target-confirmed",
        action="store_true",
        help="operator has manually confirmed the currently-open chat is the target; "
             "skip UIA/visual target verification. Manual supervised send only; do NOT use for the unattended queue.",
    )
    parser.add_argument("--search-mode", choices=["enter", "click", "none"], default="none")
    parser.add_argument("--submit-method", choices=["button", "enter"], default="button")
    parser.add_argument("--automation", choices=["uia", "win32"], default="uia")
    parser.add_argument("--settle-seconds", type=float, default=0.7)
    parser.add_argument("--allow-voice-file-fallback", action="store_true")
    parser.add_argument(
        "--allow-stable-id-name-search",
        action="store_true",
        help="allow stable wxid queue items to search by visible display name; disabled by default to avoid wrong chats",
    )
    parser.add_argument("--account-id", default="wechat-main")
    parser.add_argument("--heartbeat-interval", type=float, default=30.0)
    parser.add_argument("--heartbeat-ttl", type=float, default=90.0)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("heartbeat")
    p.set_defaults(func=run_heartbeat)

    p = sub.add_parser("probe")
    p.set_defaults(func=run_probe)

    p = sub.add_parser("target-probe")
    p.add_argument("--target", required=True, help="visible chat display name to compare with the current chat")
    p.set_defaults(func=run_target_probe)

    p = sub.add_parser("capture")
    p.add_argument("--output", default=r"..\runtime\data\pc-weixin-captures\latest.png")
    p.set_defaults(func=run_capture)

    p = sub.add_parser("poll-once")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--ack-dry-run", action="store_true")
    p.add_argument("--poll-interval", type=float, default=2.0)
    p.set_defaults(func=lambda args: run_poll(args, once=True))

    p = sub.add_parser("watch")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--ack-dry-run", action="store_true")
    p.add_argument("--poll-interval", type=float, default=2.0)
    p.set_defaults(func=lambda args: run_poll(args, once=False))

    p = sub.add_parser("send-test")
    p.add_argument("--to", required=True)
    p.add_argument("--chat-id", default="")
    p.add_argument("--text", default="PC Weixin bridge test")
    p.set_defaults(func=run_send_test)

    p = sub.add_parser("send-voice-test")
    p.add_argument("--to", required=True)
    p.add_argument("--chat-id", default="")
    p.add_argument("--text", default="PC Weixin native voice bubble test")
    p.add_argument("--audio-path", default="", help="optional wav file to play while WeChat records the native voice bubble")
    p.add_argument("--duration-seconds", type=float, default=VOICE_DEFAULT_RECORD_SECONDS)
    p.set_defaults(func=run_send_voice_test)

    p = sub.add_parser("audio-route")
    p.add_argument("--audio-path", default="", help="optional wav file to validate for native voice bubble content")
    p.set_defaults(func=run_audio_route)

    p = sub.add_parser("audio-loopback-diagnose")
    p.add_argument("--audio-path", required=True, help="wav file to play through the route candidate")
    p.add_argument("--output", default="", help="optional wav file path for the captured diagnostic")
    p.set_defaults(func=run_audio_loopback_diagnose)

    p = sub.add_parser("audio-loopback-diagnose-all")
    p.add_argument("--audio-path", required=True, help="wav file to play through every route candidate")
    p.add_argument("--output-dir", default="", help="directory for captured diagnostic wav files")
    p.add_argument("--max-attempts", type=int, default=12)
    p.set_defaults(func=run_audio_loopback_diagnose_all)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except BridgeError as exc:
        print_json({"ok": False, "error": str(exc)}, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
