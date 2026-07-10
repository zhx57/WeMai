import base64
import json
import os
import subprocess
import sys
import wave
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

import native_voice


def _wav_bytes(seconds=0.05, rate=8000):
    import io
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(b"\0\0" * int(seconds * rate))
    return output.getvalue()


def test_voice_base64_is_materialized_as_wav(tmp_path, monkeypatch):
    import wx_Processer
    monkeypatch.setattr(wx_Processer, "NATIVE_VOICE_WAV_DIR", str(tmp_path))
    monkeypatch.setattr(wx_Processer.subprocess, "run", mock.Mock(side_effect=FileNotFoundError))
    processor = wx_Processer.MessageProcessor.__new__(wx_Processer.MessageProcessor)
    path, temporary = processor._prepare_voice({"base64": base64.b64encode(_wav_bytes()).decode()})
    try:
        assert temporary is True
        assert Path(path).parent == tmp_path
        with wave.open(path, "rb") as audio:
            assert audio.getsampwidth() == 2
            assert audio.getnframes() > 0
    finally:
        os.unlink(path)


def test_voice_segment_path_is_parsed(monkeypatch, tmp_path):
    import wx_Processer
    source = tmp_path / "voice.wav"
    source.write_bytes(_wav_bytes())
    processor = wx_Processer.MessageProcessor.__new__(wx_Processer.MessageProcessor)
    monkeypatch.setattr(processor, "_convert_to_wav", lambda src, dst: Path(dst).write_bytes(Path(src).read_bytes()))
    path, temporary = processor._prepare_voice({"path": str(source)})
    try:
        assert temporary and path.endswith(".wav")
        assert native_voice.wav_duration(path) > 0
    finally:
        os.unlink(path)


def test_native_voice_config_defaults_and_override():
    code = (
        "import json,config; print(json.dumps({"
        "'enabled':config.NATIVE_VOICE_ENABLED,"
        "'x':config.NATIVE_VOICE_VOICE_START_X,"
        "'fallback':config.NATIVE_VOICE_VOICE_FALLBACK_TO_FILE}))"
    )
    env = os.environ.copy()
    env.update({"NATIVE_VOICE_ENABLED": "true", "NATIVE_VOICE_VOICE_START_X": "0.75",
                "NATIVE_VOICE_VOICE_FALLBACK_TO_FILE": "false"})
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parents[1],
                            env=env, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"enabled": True, "x": 0.75, "fallback": False}


def test_route_status_is_mockable_and_reports_ready(monkeypatch):
    devices = [
        {"index": 1, "name": "CABLE Output (VB-Audio)", "max_input_channels": 2,
         "max_output_channels": 0, "default_samplerate": 48000.0},
        {"index": 2, "name": "CABLE Input (VB-Audio)", "max_input_channels": 0,
         "max_output_channels": 2, "default_samplerate": 48000.0},
    ]
    monkeypatch.setattr(native_voice, "_audio_devices", lambda: {"ok": True, "devices": devices})
    monkeypatch.setattr(native_voice, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(native_voice, "_default_endpoint_ids", lambda: {0: "physical mic"})
    status = native_voice.diagnose_native_voice_route()
    assert status["ok"] is True
    assert status["virtual_capture"][0]["index"] == 1
    assert status["possibly_left_on_virtual_capture"] is False


def test_non_windows_route_status_degrades(monkeypatch):
    monkeypatch.setattr(native_voice, "_audio_devices",
                        lambda: {"ok": False, "devices": [], "error": "missing"})
    monkeypatch.setattr(native_voice, "os", SimpleNamespace(name="posix"))
    status = native_voice.diagnose_native_voice_route()
    assert status["ok"] is False
    assert status["error"] == "missing"
