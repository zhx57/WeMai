"""语音识别(STT)：把语音文件发去 OpenAI 兼容 /audio/transcriptions 转文字。

适配本地 whisper 服务(faster-whisper-server / whisper.cpp server 等暴露该接口)或官方 API。
仅在 config.asr.enabled 时使用；失败返回空字符串，不影响主链路。
"""

from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


def resolve_asr_api_key(settings: Any) -> str:
    secret_file = str(getattr(settings, "api_key_secret_file", "") or "")
    if secret_file:
        try:
            key = Path(secret_file).read_text(encoding="utf-8").strip()
            if key:
                return key
        except OSError:
            pass
    return str(os.environ.get(str(getattr(settings, "api_key_env", "") or ""), "") or "")


def _transcription_url(base_url: str) -> str:
    base = str(base_url or "").rstrip("/")
    if base.endswith("/audio/transcriptions"):
        return base
    return base + "/audio/transcriptions"


def _multipart(fields: dict[str, str], file_field: str, filename: str, file_bytes: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = "----waa" + uuid.uuid4().hex
    out = bytearray()
    for key, value in fields.items():
        out += f"--{boundary}\r\n".encode()
        out += f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode()
        out += f"{value}\r\n".encode("utf-8")
    out += f"--{boundary}\r\n".encode()
    out += f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode("utf-8")
    out += f"Content-Type: {content_type}\r\n\r\n".encode()
    out += file_bytes + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def transcribe(settings: Any, file_path: str | Path, *, api_key: str | None = None) -> dict[str, Any]:
    """转写一个语音文件，返回 {ok, text, error}。"""
    path = Path(file_path)
    if not path.is_file():
        return {"ok": False, "text": "", "error": f"file not found: {path}"}
    base_url = str(getattr(settings, "base_url", "") or "").strip()
    if not base_url:
        return {"ok": False, "text": "", "error": "asr.base_url not configured"}
    try:
        data = path.read_bytes()
    except OSError as exc:
        return {"ok": False, "text": "", "error": f"read failed: {exc}"}

    fields = {"model": str(getattr(settings, "model", "whisper-1") or "whisper-1"), "response_format": "json"}
    language = str(getattr(settings, "language", "") or "").strip()
    if language:
        fields["language"] = language
    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    body, ctype = _multipart(fields, "file", path.name, data, content_type)

    key = api_key if api_key is not None else resolve_asr_api_key(settings)
    headers = {"Content-Type": ctype}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(_transcription_url(base_url), data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=float(getattr(settings, "timeout_seconds", 60) or 60)) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return {"ok": False, "text": "", "error": f"{exc.code} {exc.read().decode('utf-8', 'replace')[:200]}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "text": "", "error": str(exc)}
    text = ""
    try:
        parsed = json.loads(raw)
        text = str(parsed.get("text") or "").strip() if isinstance(parsed, dict) else ""
    except json.JSONDecodeError:
        text = raw.strip()
    return {"ok": bool(text), "text": text, "error": "" if text else "empty transcription"}
