from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import urllib.request
import base64
import shutil
import wave
import uuid
from pathlib import Path
from typing import Any

from .config import AppConfig, resolve_relative


class TTSClient:
    def __init__(self, config: AppConfig):
        self.config = config

    def synthesize(self, text: str) -> Path:
        settings = self.config.media.tts
        if settings.provider == "windows_sapi":
            output = self._synthesize_windows_sapi(text)
        elif settings.provider in {"cosyvoice2", "cosyvoice3"}:
            output = self._synthesize_cosyvoice2(text)
        elif settings.provider in {"local_http_json", "gpt_sovits"}:
            output = self._synthesize_local_http(text)
        elif settings.provider == "minimax":
            output = self._synthesize_minimax(text)
        elif settings.provider == "elevenlabs":
            output = self._synthesize_elevenlabs(text)
        elif settings.provider in {"openai_compatible", "openai_tts", "siliconflow_tts", "azure_tts"}:
            # 走 OpenAI /audio/speech 协议的云端 TTS(OpenAI 官方 / SiliconFlow / 中转站等)。
            output = self._synthesize_openai_compatible(text)
        else:
            raise RuntimeError(f"unsupported TTS provider: {settings.provider}")
        # Trim leading/trailing silence so the native WeChat voice bubble does not
        # open/close with a dead gap (see 微信原生语音气泡操作说明). Best-effort: any
        # failure leaves the original file untouched. Disable with WECHAT_TTS_TRIM_SILENCE=0.
        if os.environ.get("WECHAT_TTS_TRIM_SILENCE", "1").strip().lower() not in {"0", "false", "no", "off"}:
            trim_wav_silence(output)
        return output

    def _synthesize_openai_compatible(self, text: str) -> Path:
        settings = self.config.media.tts
        if not settings.base_url:
            raise RuntimeError("media.tts.base_url is empty; cannot generate voice audio")

        output = self._output_path(text, settings.response_format)

        api_key = resolve_tts_api_key(settings)
        endpoint = settings.endpoint_path or "/audio/speech"
        url = join_endpoint_url(settings.base_url, endpoint)
        payload = expand_template(settings.request_template or {
            "model": settings.model,
            "voice": settings.voice,
            "input": text,
            "response_format": settings.response_format,
        }, {
            "text": text,
            "input": text,
            "model": settings.model,
            "voice": settings.voice,
            "response_format": settings.response_format,
        })
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=settings.timeout_seconds) as response:
            output.write_bytes(response.read())
        validate_audio_file(output)
        return output

    def _synthesize_minimax(self, text: str) -> Path:
        # MiniMax T2A v2：自家协议(非 OpenAI)。GroupId 写在 base_url/endpoint 的 ?GroupId= 里。
        # 取 PCM(hex) 再包成 WAV，桥才能播进 VB-CABLE。
        settings = self.config.media.tts
        if not settings.base_url:
            raise RuntimeError("media.tts.base_url is empty; cannot call MiniMax TTS")
        output = self._output_path(text, "wav")
        endpoint = settings.endpoint_path or default_endpoint_path("minimax")
        url = join_endpoint_url(settings.base_url, endpoint)
        template = settings.request_template or default_request_template("minimax")
        payload = expand_template(template, {
            "text": text, "input": text, "model": settings.model,
            "voice": settings.voice, "response_format": settings.response_format,
        })
        sample_rate = 32000
        try:
            sample_rate = int(((payload.get("audio_setting") or {}) if isinstance(payload, dict) else {}).get("sample_rate") or 32000)
        except (TypeError, ValueError):
            sample_rate = 32000
        api_key = resolve_tts_api_key(settings)
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=settings.timeout_seconds) as response:
            body = response.read()
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception as exc:
            raise RuntimeError(f"MiniMax TTS returned non-JSON ({exc}): {body[:200]!r}")
        audio_hex = ""
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            audio_hex = str(data["data"].get("audio") or "")
        if not audio_hex:
            raise RuntimeError(f"MiniMax TTS response has no data.audio: {str(data)[:300]}")
        try:
            pcm = bytes.fromhex(audio_hex)
        except ValueError as exc:
            raise RuntimeError(f"MiniMax TTS audio is not hex PCM ({exc}); set audio_setting.format='pcm'")
        write_pcm16_mono_wav(output, pcm, sample_rate)
        validate_audio_file(output)
        return output

    def _synthesize_elevenlabs(self, text: str) -> Path:
        # ElevenLabs：voice_id 在 URL 路径(endpoint 的 {voice})，鉴权用 xi-api-key 头。
        # 取 PCM(?output_format=pcm_44100) 再包成 WAV，桥才能播。
        settings = self.config.media.tts
        if not settings.base_url:
            raise RuntimeError("media.tts.base_url is empty; cannot call ElevenLabs TTS")
        output = self._output_path(text, "wav")
        endpoint = (settings.endpoint_path or default_endpoint_path("elevenlabs")).replace("{voice}", str(settings.voice or ""))
        url = join_endpoint_url(settings.base_url, endpoint)
        if "output_format=" not in url:
            url += ("&" if "?" in url else "?") + "output_format=pcm_44100"
        template = settings.request_template or default_request_template("elevenlabs")
        payload = expand_template(template, {
            "text": text, "input": text, "model": settings.model,
            "voice": settings.voice, "response_format": settings.response_format,
        })
        api_key = resolve_tts_api_key(settings)
        headers = {"Content-Type": "application/json", "Accept": "audio/basic"}
        if api_key:
            headers["xi-api-key"] = api_key
        request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=settings.timeout_seconds) as response:
            pcm = response.read()
        write_pcm16_mono_wav(output, pcm, 44100)
        validate_audio_file(output)
        return output

    def _synthesize_cosyvoice2(self, text: str) -> Path:
        settings = self.config.media.tts
        if not settings.base_url:
            raise RuntimeError(f"media.tts.base_url is empty; cannot call {settings.provider} local TTS")

        endpoint = settings.endpoint_path or default_endpoint_path(settings.provider)
        if cosyvoice_endpoint_looks_openai_compatible(endpoint, settings.request_template):
            return self._synthesize_openai_compatible(text)
        return self._synthesize_cosyvoice2_zero_shot(text)

    def _synthesize_cosyvoice2_zero_shot(self, text: str) -> Path:
        settings = self.config.media.tts
        output = self._output_path(text, "wav")
        endpoint = settings.endpoint_path or default_endpoint_path(settings.provider)
        url = join_endpoint_url(settings.base_url, endpoint)
        template = settings.request_template or default_request_template(settings.provider)
        payload = expand_template(template, {
            "text": text,
            "input": text,
            "model": settings.model,
            "voice": settings.voice,
            "response_format": "wav",
        })
        if not isinstance(payload, dict):
            raise RuntimeError("CosyVoice2 request_template must render to a JSON object")
        prompt_wav = str(payload.pop("prompt_wav", "") or settings.voice or "").strip()
        prompt_path = resolve_tts_upload_path(prompt_wav, self.config.path)
        sample_rate = cosyvoice_raw_sample_rate(payload)
        payload.pop("sample_rate", None)
        payload.pop("raw_sample_rate", None)
        payload.pop("output_sample_rate", None)
        payload.pop("_sample_rate", None)
        fields = {str(key): form_value(value) for key, value in payload.items() if value is not None}
        body, content_type = multipart_form_data(
            fields,
            {"prompt_wav": prompt_path},
        )
        headers = {"Content-Type": content_type}
        api_key = resolve_tts_api_key(settings)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=settings.timeout_seconds) as response:
            response_body = response.read()
            response_content_type = response.headers.get("content-type", "")
        write_cosyvoice_response(output, response_body, response_content_type, sample_rate)
        validate_audio_file(output)
        return output

    def _synthesize_local_http(self, text: str) -> Path:
        settings = self.config.media.tts
        if not settings.base_url:
            raise RuntimeError(f"media.tts.base_url is empty; cannot call local TTS provider: {settings.provider}")
        output = self._output_path(text, settings.response_format)
        endpoint = settings.endpoint_path or default_endpoint_path(settings.provider)
        url = settings.base_url.rstrip("/") + "/" + endpoint.lstrip("/")
        template = settings.request_template or default_request_template(settings.provider)
        payload = expand_template(template, {
            "text": text,
            "model": settings.model,
            "voice": settings.voice,
            "response_format": settings.response_format,
        })
        headers = {"Content-Type": "application/json"}
        api_key = resolve_tts_api_key(settings)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=settings.timeout_seconds) as response:
            body = response.read()
            content_type = response.headers.get("content-type", "")
        write_tts_response(output, body, content_type)
        validate_audio_file(output)
        return output

    def _synthesize_windows_sapi(self, text: str) -> Path:
        settings = self.config.media.tts
        if os.name != "nt":
            raise RuntimeError("windows_sapi TTS is only available on Windows")
        output = self._output_path(text, "wav")
        script = r"""
param(
  [string]$Text,
  [string]$Output,
  [string]$Voice
)
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$synth.Rate = 0
$synth.Volume = 100
if ($Voice -and $Voice -ne "alloy") {
  $match = $synth.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Name -like "*$Voice*" } | Select-Object -First 1
  if ($match) {
    $synth.SelectVoice($match.VoiceInfo.Name)
  }
}
$synth.SetOutputToWaveFile($Output)
$synth.Speak($Text)
$synth.Dispose()
"""
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as handle:
                handle.write(script)
                temp_path = Path(handle.name)
            subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(temp_path),
                    "-Text",
                    text,
                    "-Output",
                    str(output),
                    "-Voice",
                    settings.voice,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=settings.timeout_seconds,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise RuntimeError(f"windows_sapi TTS failed: {detail}") from exc
        finally:
            if temp_path:
                temp_path.unlink(missing_ok=True)
        if not output.exists():
            raise RuntimeError(f"windows_sapi TTS did not create audio file: {output}")
        validate_audio_file(output)
        return output

    def _output_path(self, text: str, extension: str) -> Path:
        settings = self.config.media.tts
        output_dir = resolve_relative(self.config.path, self.config.media.voice_output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        seed = "\n".join([settings.provider, settings.model, settings.voice, extension, text])
        digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
        return output_dir / f"voice-{digest}.{extension.lstrip('.')}"


def resolve_tts_api_key(settings) -> str:
    secret_file = str(getattr(settings, "api_key_secret_file", "") or "").strip()
    if secret_file:
        path = Path(secret_file)
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return os.environ.get(str(getattr(settings, "api_key_env", "") or "OPENAI_API_KEY"), "").strip()


def default_endpoint_path(provider: str) -> str:
    if provider == "gpt_sovits":
        return "/tts"
    if provider in {"cosyvoice2", "cosyvoice3", "openai_tts", "siliconflow_tts", "azure_tts", "openai_compatible"}:
        return "/audio/speech"
    if provider == "minimax":
        return "/t2a_v2"
    if provider == "elevenlabs":
        return "/text-to-speech/{voice}"
    return "/tts"


def default_request_template(provider: str) -> dict[str, Any]:
    if provider == "gpt_sovits":
        return {
            "text": "{text}",
            "text_lang": "zh",
            "ref_audio_path": "{voice}",
            "prompt_text": "{model}",
            "prompt_lang": "zh",
            "media_type": "{response_format}",
            "streaming_mode": False,
        }
    if provider in {"cosyvoice2", "cosyvoice3", "openai_tts", "siliconflow_tts", "azure_tts", "openai_compatible"}:
        return {
            "model": "{model}",
            "voice": "{voice}",
            "input": "{text}",
            "response_format": "{response_format}",
            "speed": 1.0,
        }
    if provider == "minimax":
        # MiniMax T2A v2；取 PCM 便于包成 WAV(桥需要 WAV)。GroupId 写在 base_url/endpoint 的 ?GroupId=。
        return {
            "model": "{model}",
            "text": "{text}",
            "stream": False,
            "voice_setting": {"voice_id": "{voice}", "speed": 1.0, "vol": 1.0, "pitch": 0},
            "audio_setting": {"sample_rate": 32000, "format": "pcm", "channel": 1},
        }
    if provider == "elevenlabs":
        return {
            "text": "{text}",
            "model_id": "{model}",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }
    return {
        "text": "{text}",
        "voice": "{voice}",
        "format": "{response_format}",
    }


def expand_template(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        rendered = value
        for key, replacement in variables.items():
            rendered = rendered.replace("{" + key + "}", replacement)
        return rendered
    if isinstance(value, list):
        return [expand_template(item, variables) for item in value]
    if isinstance(value, dict):
        return {str(key): expand_template(item, variables) for key, item in value.items()}
    return value


def write_tts_response(output: Path, body: bytes, content_type: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if "json" not in content_type.lower():
        output.write_bytes(body)
        return
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("local TTS returned JSON, but it was not an object")
    audio_b64 = str(payload.get("audio_base64") or payload.get("audio") or "").strip()
    if audio_b64:
        output.write_bytes(base64.b64decode(audio_b64))
        return
    source = str(payload.get("path") or payload.get("file") or payload.get("audio_path") or "").strip()
    if source:
        source_path = Path(source)
        if not source_path.exists():
            raise RuntimeError(f"local TTS returned audio path that does not exist: {source}")
        shutil.copyfile(source_path, output)
        return
    raise RuntimeError("local TTS JSON response did not include binary audio, audio_base64, or file path")


def join_endpoint_url(base_url: str, endpoint_path: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    endpoint = "/" + str(endpoint_path or "").strip().lstrip("/")
    if base.endswith("/v1") and endpoint.startswith("/v1/"):
        endpoint = endpoint[3:]
    return base + endpoint


def cosyvoice_endpoint_looks_openai_compatible(endpoint_path: str, template: dict[str, Any] | None = None) -> bool:
    endpoint = "/" + str(endpoint_path or "").strip().lstrip("/")
    if endpoint.endswith("/audio/speech"):
        return True
    keys = {str(key) for key in dict(template or {}).keys()}
    return "input" in keys and "prompt_wav" not in keys


def form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def cosyvoice_raw_sample_rate(payload: dict[str, Any]) -> int:
    for key in ("sample_rate", "raw_sample_rate", "output_sample_rate", "_sample_rate"):
        value = payload.get(key)
        if value not in (None, ""):
            try:
                sample_rate = int(float(str(value)))
            except ValueError:
                break
            if sample_rate > 0:
                return sample_rate
    return 24000


def resolve_tts_upload_path(raw_path: str, config_path: Path | None = None) -> Path:
    value = str(raw_path or "").strip().strip('"')
    if not value:
        raise RuntimeError("CosyVoice2 prompt_wav/voice is empty; configure a prompt WAV path first")
    candidates: list[Path] = []
    raw = Path(value)
    candidates.append(raw)
    if not raw.is_absolute() and config_path is not None:
        candidates.append(resolve_relative(config_path, value))
    translated = wsl_mount_path_to_windows(value)
    if translated:
        candidates.append(translated)
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    rendered = ", ".join(str(item) for item in candidates)
    raise RuntimeError(f"CosyVoice2 prompt WAV does not exist; checked: {rendered}")


def wsl_mount_path_to_windows(path: str) -> Path | None:
    value = str(path or "").strip()
    normalized = value.replace("\\", "/")
    if not normalized.startswith("/mnt/") or len(normalized) < 7:
        return None
    drive = normalized[5]
    if normalized[6] != "/" or not drive.isalpha():
        return None
    rest = normalized[7:].replace("/", "\\")
    return Path(f"{drive.upper()}:\\{rest}")


def multipart_form_data(fields: dict[str, str], files: dict[str, Path]) -> tuple[bytes, str]:
    boundary = "----wechat-agent-adapter-" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    for key, path in files.items():
        filename = path.name
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
                "Content-Type: audio/wav\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def write_cosyvoice_response(output: Path, body: bytes, content_type: str, sample_rate: int) -> None:
    lower = str(content_type or "").lower()
    output.parent.mkdir(parents=True, exist_ok=True)
    if "json" in lower:
        write_tts_response(output, body, content_type)
        return
    if body[:4] == b"RIFF" or "wav" in lower or "wave" in lower:
        output.write_bytes(body)
        return
    write_pcm16_mono_wav(output, body, sample_rate)


def write_pcm16_mono_wav(output: Path, pcm: bytes, sample_rate: int) -> None:
    if len(pcm) % 2:
        pcm = pcm[:-1]
    if not pcm:
        raise RuntimeError("CosyVoice2 returned empty PCM audio")
    with wave.open(str(output), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)


def trim_wav_silence(
    path: Path,
    *,
    keep_pad_seconds: float = 0.06,
    threshold_ratio: float = 0.02,
    min_abs_threshold: int = 256,
) -> dict[str, Any]:
    """Trim leading/trailing near-silence from a 16-bit PCM WAV, in place.

    Best-effort and dependency-free (stdlib wave + array). Non-WAV or non-16-bit
    input, or any error, leaves the file untouched and reports trimmed=False.
    """
    import array

    try:
        if str(path).lower().rsplit(".", 1)[-1] != "wav":
            return {"trimmed": False, "reason": "not a wav file"}
        with wave.open(str(path), "rb") as handle:
            nchannels = handle.getnchannels()
            sampwidth = handle.getsampwidth()
            framerate = handle.getframerate()
            nframes = handle.getnframes()
            raw = handle.readframes(nframes)
        if sampwidth != 2 or nchannels < 1 or framerate <= 0:
            return {"trimmed": False, "reason": f"unsupported format sw={sampwidth} ch={nchannels}"}
        samples = array.array("h")
        samples.frombytes(raw)
        total_frames = len(samples) // nchannels
        if total_frames <= 0:
            return {"trimmed": False, "reason": "no frames"}
        peak = 0
        for value in samples:
            av = value if value >= 0 else -value
            if av > peak:
                peak = av
        if peak == 0:
            return {"trimmed": False, "reason": "all silence"}
        threshold = max(int(min_abs_threshold), int(peak * threshold_ratio))
        first = last = -1
        for frame in range(total_frames):
            base = frame * nchannels
            frame_peak = 0
            for channel in range(nchannels):
                av = samples[base + channel]
                if av < 0:
                    av = -av
                if av > frame_peak:
                    frame_peak = av
            if frame_peak >= threshold:
                if first == -1:
                    first = frame
                last = frame
        if first == -1:
            return {"trimmed": False, "reason": "no audio above threshold"}
        pad = int(keep_pad_seconds * framerate)
        start = max(0, first - pad)
        end = min(total_frames, last + 1 + pad)
        if start <= 0 and end >= total_frames:
            return {"trimmed": False, "reason": "nothing to trim"}
        kept = samples[start * nchannels:end * nchannels]
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(nchannels)
            handle.setsampwidth(sampwidth)
            handle.setframerate(framerate)
            handle.writeframes(kept.tobytes())
        return {
            "trimmed": True,
            "frame_rate": framerate,
            "original_frames": total_frames,
            "kept_frames": end - start,
            "removed_lead_seconds": round(start / framerate, 3),
            "removed_tail_seconds": round((total_frames - end) / framerate, 3),
            "kept_seconds": round((end - start) / framerate, 3),
        }
    except Exception as exc:
        return {"trimmed": False, "reason": f"trim_failed: {exc}"}


def validate_audio_file(path: Path) -> None:
    if not path.exists():
        raise RuntimeError(f"TTS audio file does not exist: {path}")
    size = path.stat().st_size
    if size <= 128:
        raise RuntimeError(f"TTS audio file is empty or too small: {path} ({size} bytes)")
    if path.suffix.lower() != ".wav":
        return
    try:
        with wave.open(str(path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
            duration = frames / rate if rate else 0.0
    except wave.Error as exc:
        raise RuntimeError(f"TTS WAV file is invalid: {path}: {exc}") from exc
    if frames <= 0 or duration <= 0:
        raise RuntimeError(f"TTS WAV file has no audible frames: {path}")
