"""微信 4.x 原生语音条后端。

该模块不依赖 wxauto 控件树：wxauto 只负责打开目标聊天，本模块通过窗口矩形、
Windows Core Audio 和 PortAudio 完成实时录音。所有 Windows/可选依赖均延迟导入。
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import os
import threading
import time
import wave
from ctypes import POINTER, Structure, Union, c_int, c_long, c_longlong, c_size_t, c_void_p
from ctypes.wintypes import BOOL, DWORD, LPWSTR
from pathlib import Path

from config import (
    NATIVE_VOICE_MAX_RECORD_SECONDS,
    NATIVE_VOICE_REQUIRE_CONTENT_PROVEN,
    NATIVE_VOICE_VIRTUAL_CAPTURE_KEYWORDS,
    NATIVE_VOICE_VOICE_CANCEL_X,
    NATIVE_VOICE_VOICE_CANCEL_Y,
    NATIVE_VOICE_VOICE_SEND_X,
    NATIVE_VOICE_VOICE_SEND_Y,
    NATIVE_VOICE_VOICE_START_X,
    NATIVE_VOICE_VOICE_START_Y,
)

logger = logging.getLogger(__name__)
_ROLES = (0, 1, 2)  # Console / Multimedia / Communications
_active_sessions = []
_sessions_lock = threading.RLock()


def _enable_dpi_awareness():
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor DPI aware.
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_enable_dpi_awareness()


class NativeVoiceError(RuntimeError):
    pass


def detect_wechat_version():
    """尽力读取运行中 WeChat.exe 文件版本，失败时返回 None。"""
    if os.name != "nt":
        return None
    try:
        import psutil
        import win32api
        for process in psutil.process_iter(["name", "exe"]):
            if str(process.info.get("name") or "").lower() in {"wechat.exe", "weixin.exe"}:
                info = win32api.GetFileVersionInfo(process.info["exe"], "\\")
                ms, ls = info["FileVersionMS"], info["FileVersionLS"]
                return (ms >> 16, ms & 0xffff, ls >> 16, ls & 0xffff)
    except Exception:
        logger.debug("微信版本检测失败", exc_info=True)
    return None


def wav_duration(path):
    with wave.open(str(path), "rb") as audio:
        if audio.getframerate() <= 0:
            raise ValueError("WAV 采样率无效")
        return audio.getnframes() / float(audio.getframerate())


def _audio_devices():
    try:
        import sounddevice as sd
        devices = []
        for index, item in enumerate(sd.query_devices()):
            devices.append({"index": index, "name": str(item.get("name") or ""),
                            "max_input_channels": int(item.get("max_input_channels") or 0),
                            "max_output_channels": int(item.get("max_output_channels") or 0),
                            "default_samplerate": float(item.get("default_samplerate") or 0)})
        return {"ok": True, "devices": devices}
    except Exception as exc:
        return {"ok": False, "devices": [], "error": str(exc)}


def _virtual_devices(devices=None):
    listing = devices if devices is not None else _audio_devices().get("devices", [])
    capture_words = tuple(word.lower() for word in NATIVE_VOICE_VIRTUAL_CAPTURE_KEYWORDS)
    capture = [item for item in listing if item["max_input_channels"] > 0 and
               any(word in item["name"].lower() for word in capture_words)]
    render_words = ("cable input", "vb-audio", "voicemeeter input", "virtual audio")
    render = [item for item in listing if item["max_output_channels"] > 0 and
              any(word in item["name"].lower() for word in render_words)]
    return capture, render


def diagnose_native_voice_route():
    """返回可序列化诊断信息；非 Windows 和缺依赖都只报告失败，不抛异常。"""
    pythoncom = None
    if os.name == "nt":
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except ImportError:
            pythoncom = None
    try:
        listing = _audio_devices()
        capture, render = _virtual_devices(listing.get("devices", []))
        current = _default_endpoint_ids() if os.name == "nt" else {}
        current_names = " ".join(
            (_endpoint_display_name(value) or str(value)).lower() for value in current.values()
        )
        capture_words = tuple(word.lower() for word in NATIVE_VOICE_VIRTUAL_CAPTURE_KEYWORDS)
        return {
            "ok": os.name == "nt" and listing["ok"] and bool(capture and render),
            "platform": os.name,
            "dependencies_ready": listing["ok"],
            "virtual_capture": capture,
            "virtual_render": render,
            "current_capture_defaults": current,
            "possibly_left_on_virtual_capture": any(word in current_names for word in capture_words),
            "error": listing.get("error") if not listing["ok"] else None,
        }
    finally:
        if pythoncom is not None:
            pythoncom.CoUninitialize()


def _core_audio_objects():
    import comtypes.client
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

    class IMMDevice(IUnknown):
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"), (["in"], DWORD, "ctx"), (["in"], c_void_p, "params"), (["out"], POINTER(c_void_p), "out")),
            COMMETHOD([], HRESULT, "OpenPropertyStore", (["in"], DWORD, "access"), (["out"], POINTER(c_void_p), "props")),
            COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(LPWSTR), "device_id")),
            COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(DWORD), "state")),
        ]

    class IMMDeviceEnumerator(IUnknown):
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = [
            COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], c_int, "flow"), (["in"], DWORD, "mask"), (["out"], POINTER(c_void_p), "devices")),
            COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], c_int, "flow"), (["in"], c_int, "role"), (["out"], POINTER(POINTER(IMMDevice)), "endpoint")),
            COMMETHOD([], HRESULT, "GetDevice", (["in"], LPWSTR, "id"), (["out"], POINTER(POINTER(IMMDevice)), "device")),
            COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback", (["in"], c_void_p, "callback")),
            COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback", (["in"], c_void_p, "callback")),
        ]

    class IPolicyConfig(IUnknown):
        _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetMixFormat", (["in"], LPWSTR, "id"), (["out"], POINTER(c_void_p), "fmt")),
            COMMETHOD([], HRESULT, "GetDeviceFormat", (["in"], LPWSTR, "id"), (["in"], BOOL, "default"), (["out"], POINTER(c_void_p), "fmt")),
            COMMETHOD([], HRESULT, "ResetDeviceFormat", (["in"], LPWSTR, "id")),
            COMMETHOD([], HRESULT, "SetDeviceFormat", (["in"], LPWSTR, "id"), (["in"], c_void_p, "endpoint"), (["in"], c_void_p, "mix")),
            COMMETHOD([], HRESULT, "GetProcessingPeriod", (["in"], LPWSTR, "id"), (["in"], BOOL, "default"), (["out"], POINTER(c_longlong), "defperiod"), (["out"], POINTER(c_longlong), "minperiod")),
            COMMETHOD([], HRESULT, "SetProcessingPeriod", (["in"], LPWSTR, "id"), (["in"], POINTER(c_longlong), "period")),
            COMMETHOD([], HRESULT, "GetShareMode", (["in"], LPWSTR, "id"), (["out"], c_void_p, "mode")),
            COMMETHOD([], HRESULT, "SetShareMode", (["in"], LPWSTR, "id"), (["in"], c_void_p, "mode")),
            COMMETHOD([], HRESULT, "GetPropertyValue", (["in"], LPWSTR, "id"), (["in"], c_void_p, "key"), (["out"], c_void_p, "value")),
            COMMETHOD([], HRESULT, "SetPropertyValue", (["in"], LPWSTR, "id"), (["in"], c_void_p, "key"), (["in"], c_void_p, "value")),
            COMMETHOD([], HRESULT, "SetDefaultEndpoint", (["in"], LPWSTR, "id"), (["in"], c_int, "role")),
            COMMETHOD([], HRESULT, "SetEndpointVisibility", (["in"], LPWSTR, "id"), (["in"], BOOL, "visible")),
        ]

    enumerator = comtypes.client.CreateObject(GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"), interface=IMMDeviceEnumerator)
    policy = comtypes.client.CreateObject(GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}"), interface=IPolicyConfig)
    return enumerator, policy


def _default_endpoint_ids():
    if os.name != "nt":
        return {}
    try:
        enumerator, _ = _core_audio_objects()
        return {role: str(enumerator.GetDefaultAudioEndpoint(1, role).GetId()) for role in _ROLES}
    except Exception as exc:
        logger.debug("读取默认录音设备失败: %s", exc)
        return {}


def _set_defaults(endpoint_id):
    _, policy = _core_audio_objects()
    for role in _ROLES:
        policy.SetDefaultEndpoint(endpoint_id, role)


def _find_mmdevice_id(name):
    """从 MMDevices 注册表把 PortAudio 友好名映射为 Core Audio endpoint id。"""
    import winreg
    root_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
    needle = name.lower()
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root_path) as root:
        for index in range(winreg.QueryInfoKey(root)[0]):
            endpoint_id = winreg.EnumKey(root, index)
            props_path = root_path + "\\" + endpoint_id + "\\Properties"
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, props_path) as props:
                    values = [str(winreg.EnumValue(props, i)[1]) for i in range(winreg.QueryInfoKey(props)[1])]
            except OSError:
                continue
            if any(needle in value.lower() or value.lower() in needle for value in values if value):
                return "{0.0.1.00000000}." + endpoint_id
    raise NativeVoiceError(f"无法映射虚拟录音端点: {name}")


def _endpoint_display_name(endpoint_id):
    if os.name != "nt":
        return ""
    try:
        import winreg
        short_id = str(endpoint_id).rsplit(".", 1)[-1]
        path = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture" +
                "\\" + short_id + "\\Properties")
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as props:
            values = [str(winreg.EnumValue(props, i)[1]) for i in range(winreg.QueryInfoKey(props)[1])]
        return " ".join(values)
    except Exception:
        return ""


class AudioRoute:
    def __init__(self):
        self.previous = {}
        self.capture = None
        self.render = None
        self.restored = False

    def begin(self):
        if os.name != "nt":
            raise NativeVoiceError("原生语音仅支持 Windows")
        import pythoncom
        pythoncom.CoInitialize()
        try:
            listing = _audio_devices()
            if not listing["ok"]:
                raise NativeVoiceError("sounddevice/numpy 未就绪: " + listing.get("error", ""))
            captures, renders = _virtual_devices(listing["devices"])
            if not captures or not renders:
                raise NativeVoiceError("未找到成对的虚拟录音/播放设备")
            self.capture, self.render = captures[0], renders[0]
            self.previous = _default_endpoint_ids()
            if len(self.previous) != len(_ROLES):
                raise NativeVoiceError("无法保存当前默认录音设备")
            _set_defaults(_find_mmdevice_id(self.capture["name"]))
            with _sessions_lock:
                _active_sessions.append(self)
            return self
        finally:
            pythoncom.CoUninitialize()

    def restore(self):
        if self.restored:
            return True
        import pythoncom
        pythoncom.CoInitialize()
        errors = []
        try:
            _, policy = _core_audio_objects()
            for role, endpoint_id in self.previous.items():
                try:
                    policy.SetDefaultEndpoint(endpoint_id, role)
                except Exception as exc:
                    errors.append(str(exc))
        finally:
            self.restored = not errors
            with _sessions_lock:
                if self in _active_sessions:
                    _active_sessions.remove(self)
            pythoncom.CoUninitialize()
        if errors:
            logger.error("恢复默认录音设备失败: %s", "; ".join(errors))
        return not errors


def restore_all_audio_routes():
    with _sessions_lock:
        sessions = list(reversed(_active_sessions))
    for session in sessions:
        try:
            session.restore()
        except Exception:
            logger.exception("退出恢复音频设备失败")


atexit.register(restore_all_audio_routes)


def _prepare_wav(path, render):
    import numpy as np
    import sounddevice as sd
    with wave.open(str(path), "rb") as audio:
        channels, width, rate = audio.getnchannels(), audio.getsampwidth(), audio.getframerate()
        raw = audio.readframes(audio.getnframes())
    if width != 2:
        raise NativeVoiceError("原生语音目前要求 16-bit PCM WAV")
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    data = data.reshape(-1, channels)
    target_rate = int(render["default_samplerate"] or rate)
    if rate != target_rate:
        length = max(1, round(len(data) * target_rate / rate))
        old = np.linspace(0, 1, len(data), endpoint=False)
        new = np.linspace(0, 1, length, endpoint=False)
        data = np.stack([np.interp(new, old, data[:, i]) for i in range(channels)], axis=1).astype(np.float32)
    out_channels = max(1, min(2, render["max_output_channels"]))
    if data.shape[1] != out_channels:
        mono = data.mean(axis=1, keepdims=True)
        data = np.repeat(mono, out_channels, axis=1)
    sd.play(np.zeros((max(1, target_rate // 10), out_channels), dtype=np.float32),
            samplerate=target_rate, device=render["index"], blocking=True)
    return data, target_rate


def _window_rect_and_handle(title=None):
    import win32gui
    candidates = []
    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        text = win32gui.GetWindowText(hwnd)
        cls = win32gui.GetClassName(hwnd)
        if (title and title in text) or cls in {"WeChatMainWndForPC", "ChatWnd"}:
            rect = win32gui.GetWindowRect(hwnd)
            if rect[2] - rect[0] > 500 and rect[3] - rect[1] > 400:
                candidates.append((hwnd, rect, text))
    win32gui.EnumWindows(visit, None)
    if not candidates:
        raise NativeVoiceError("未找到可用微信窗口")
    candidates.sort(key=lambda item: (not bool(title and title in item[2]), item[2] == ""))
    return candidates[0]


class _MOUSEINPUT(Structure):
    _fields_ = [
        ("dx", c_long),
        ("dy", c_long),
        ("mouseData", DWORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", c_size_t),
    ]


class _INPUTUNION(Union):
    _fields_ = [("mi", _MOUSEINPUT)]


class _INPUT(Structure):
    _anonymous_ = ("union",)
    _fields_ = [("type", DWORD), ("union", _INPUTUNION)]


def _send_mouse_input(flags):
    event = _INPUT(type=0, mi=_MOUSEINPUT(dwFlags=flags))
    sent = ctypes.windll.user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(_INPUT))
    if sent != 1:
        raise ctypes.WinError()


def _click(hwnd, rect, x_fraction, y_fraction):
    import win32api
    import win32con
    import win32gui
    user32 = ctypes.windll.user32
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    wechat_tid = user32.GetWindowThreadProcessId(hwnd, None)
    current_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    attached = bool(user32.AttachThreadInput(current_tid, wechat_tid, True))
    try:
        win32gui.SetForegroundWindow(hwnd)
        win32gui.BringWindowToTop(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(current_tid, wechat_tid, False)
    time.sleep(0.1)
    x = round(rect[0] + (rect[2] - rect[0]) * x_fraction)
    y = round(rect[1] + (rect[3] - rect[1]) * y_fraction)
    win32api.SetCursorPos((x, y))
    _send_mouse_input(win32con.MOUSEEVENTF_LEFTDOWN)
    _send_mouse_input(win32con.MOUSEEVENTF_LEFTUP)


def _compose_signature(rect):
    from PIL import ImageGrab, ImageStat
    height = rect[3] - rect[1]
    box = (rect[0], rect[1] + int(height * 0.88), rect[2], rect[3])
    image = ImageGrab.grab(bbox=box).convert("RGB").resize((64, 16))
    return image, ImageStat.Stat(image)


def _chat_signature(rect):
    from PIL import ImageGrab
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    box = (rect[0] + int(width * 0.28), rect[1] + int(height * 0.12),
           rect[2], rect[1] + int(height * 0.86))
    return ImageGrab.grab(bbox=box).convert("RGB").resize((96, 72))


def _visible_message_changed(before, rect):
    from PIL import ImageChops, ImageStat
    after = _chat_signature(rect)
    mean = ImageStat.Stat(ImageChops.difference(before, after)).mean
    return sum(mean) / len(mean) >= 0.8


def _recording_mode_changed(before, rect):
    from PIL import ImageChops, ImageStat
    after, _ = _compose_signature(rect)
    difference = ImageStat.Stat(ImageChops.difference(before, after)).mean
    return sum(difference) / len(difference) >= 3.0


class NativeVoiceSender:
    _lock = threading.RLock()

    def send(self, wav_path, chat_title=None, progress_callback=None):
        version = detect_wechat_version()
        if version is not None and version < (4, 1, 9, 0):
            raise NativeVoiceError(
                "原生语音要求微信 4.1.9+，当前版本 " + ".".join(map(str, version))
            )
        duration = wav_duration(wav_path)
        if duration <= 0 or duration > NATIVE_VOICE_MAX_RECORD_SECONDS:
            raise NativeVoiceError(
                f"WAV 时长必须在 0..{NATIVE_VOICE_MAX_RECORD_SECONDS} 秒，当前 {duration:.2f} 秒"
            )
        if NATIVE_VOICE_REQUIRE_CONTENT_PROVEN:
            proof = diagnose_native_voice_loopback(wav_path)
            if not proof.get("ok"):
                raise NativeVoiceError("虚拟声卡 loopback 验证失败: " + proof.get("error", "未知错误"))
        with self._lock:
            route = AudioRoute()
            recording = False
            try:
                route.begin()
                data, rate = _prepare_wav(wav_path, route.render)
                hwnd, rect, _ = _window_rect_and_handle(chat_title)
                chat_before = _chat_signature(rect)
                before, _ = _compose_signature(rect)
                _click(hwnd, rect, NATIVE_VOICE_VOICE_START_X, NATIVE_VOICE_VOICE_START_Y)
                recording = True
                time.sleep(0.18)
                # 关键保护：确认输入栏确实切入录音态后才播放，避免音频空耗。
                if not _recording_mode_changed(before, rect):
                    raise NativeVoiceError("点击后未检测到微信录音模式")
                import sounddevice as sd
                sd.play(data, samplerate=rate, device=route.render["index"], blocking=False)
                deadline = time.monotonic() + duration
                while time.monotonic() < deadline:
                    if progress_callback:
                        progress_callback()
                    time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
                sd.wait()
                time.sleep(0.12)
                _click(hwnd, rect, NATIVE_VOICE_VOICE_SEND_X, NATIVE_VOICE_VOICE_SEND_Y)
                recording = False
                time.sleep(0.8)
                if not _visible_message_changed(chat_before, rect):
                    raise NativeVoiceError("发送后未检测到新的聊天区气泡")
                return True
            except Exception:
                if recording:
                    try:
                        hwnd, rect, _ = _window_rect_and_handle(chat_title)
                        _click(hwnd, rect, NATIVE_VOICE_VOICE_CANCEL_X, NATIVE_VOICE_VOICE_CANCEL_Y)
                    except Exception:
                        logger.exception("取消微信录音模式失败")
                raise
            finally:
                try:
                    import sounddevice as sd
                    sd.stop()
                except Exception:
                    pass
                route.restore()


def diagnose_native_voice_loopback(wav_path):
    """短时播放/录制并用 RMS 判断虚拟声卡是否有内容。"""
    pythoncom = None
    try:
        if os.name == "nt":
            import pythoncom
            pythoncom.CoInitialize()
        import numpy as np
        import sounddevice as sd
        listing = _audio_devices()
        captures, renders = _virtual_devices(listing.get("devices", []))
        if not captures or not renders:
            return {"ok": False, "error": "未找到成对虚拟声卡"}
        data, rate = _prepare_wav(wav_path, renders[0])
        sample = data[: min(len(data), rate * 2)]
        recorded = sd.playrec(sample, samplerate=rate, channels=1,
                              device=(captures[0]["index"], renders[0]["index"]), blocking=True)
        rms = float(np.sqrt(np.mean(np.square(recorded)))) if recorded.size else 0.0
        return {"ok": rms >= 0.001, "rms": rms, "capture": captures[0], "render": renders[0]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        if pythoncom is not None:
            pythoncom.CoUninitialize()


def warn_if_stale_route():
    status = diagnose_native_voice_route()
    if status.get("possibly_left_on_virtual_capture"):
        logger.warning("默认录音设备疑似仍是虚拟声卡；可能是上次异常退出遗留，请人工检查")
    return status
