from __future__ import annotations

import json
import os
import sys
import threading
import time
import wave
from math import gcd
from pathlib import Path
from typing import Any, Iterable


MMDEVICE_NAME_PROP = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
MMDEVICE_FRIENDLY_NAME_PROP = "{a45c254e-df1c-4efd-8020-67d146a850e0},14"
MMDEVICE_PROVIDER_PROP = "{b3f8fa53-0004-438e-9003-51a46e139bfc},6"
MMDEVICE_INSTANCE_PROP = "{b3f8fa53-0004-438e-9003-51a46e139bfc},2"
MMDEVICE_DRIVER_PROP = "{83da6326-97a6-4088-9453-a1923f573b29},3"
MMDEVICE_HARDWARE_PROP = "{a8b865dd-2e3d-4094-ad97-e593a70c75d6},8"
MMDEVICE_AUDIO_ROOT = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio"
DEVICE_STATE_NAMES = {
    1: "active",
    2: "disabled",
    4: "not_present",
    8: "unplugged",
}
DEVICE_STATE_BITS = (
    (1, "active"),
    (2, "disabled"),
    (4, "not_present"),
    (8, "unplugged"),
)

LOOPBACK_CAPTURE_KEYWORDS = (
    "stereo mix",
    "立体声混音",
    "what u hear",
    "wave out mix",
    "loopback",
)
VIRTUAL_CAPTURE_KEYWORDS = (
    "cable output",
    "vb-audio",
    "voicemeeter output",
    "voicemeeter aux output",
    "virtual audio",
    "todesk virtual",
)
VIRTUAL_RENDER_KEYWORDS = (
    "cable input",
    "vb-audio",
    "voicemeeter input",
    "voicemeeter aux input",
    "virtual audio",
    "todesk virtual",
)

AUDIO_FLOWS = {
    "render": 0,
    "capture": 1,
}
AUDIO_ROLES = {
    "console": 0,
    "multimedia": 1,
    "communications": 2,
}
DEFAULT_AUDIO_ROLES = ("console", "multimedia", "communications")
MMDEVICE_FULL_ID_PREFIX = {
    "render": "{0.0.0.00000000}",
    "capture": "{0.0.1.00000000}",
}
PORTAUDIO_HOST_PRIORITY = {
    "Windows WASAPI": 50,
    "Windows DirectSound": 30,
    "MME": 20,
    "Windows WDM-KS": 10,
}
UNTRUSTED_VIRTUAL_CAPTURE_KEYWORDS = (
    "todesk virtual",
)
LOOPBACK_DIAGNOSTIC_STATE_FILE = "native-voice-loopback-latest.json"
_COM_THREAD_STATE = threading.local()


def enumerate_audio_endpoints() -> dict[str, Any]:
    if os.name != "nt":
        return {
            "ok": False,
            "platform": os.name,
            "error": "Windows audio endpoint registry is required",
            "capture": [],
            "render": [],
        }
    try:
        capture = _read_mmdevices("Capture")
        render = _read_mmdevices("Render")
    except Exception as exc:
        return {
            "ok": False,
            "platform": os.name,
            "error": str(exc),
            "capture": [],
            "render": [],
        }
    return {
        "ok": True,
        "platform": os.name,
        "capture": capture,
        "render": render,
    }


def native_voice_route_status(audio_path: str | Path | None = None) -> dict[str, Any]:
    endpoints = enumerate_audio_endpoints()
    capture = list(endpoints.get("capture") or [])
    render = list(endpoints.get("render") or [])
    active_capture = [item for item in capture if item.get("active")]
    active_render = [item for item in render if item.get("active")]
    loopback_capture = [
        item for item in active_capture if endpoint_matches_keywords(item, LOOPBACK_CAPTURE_KEYWORDS)
    ]
    virtual_capture = [
        item for item in active_capture if endpoint_matches_keywords(item, VIRTUAL_CAPTURE_KEYWORDS)
    ]
    virtual_render = [
        item for item in active_render if endpoint_matches_keywords(item, VIRTUAL_RENDER_KEYWORDS)
    ]
    file_info = audio_file_info(audio_path)
    route_candidates: list[dict[str, Any]] = []
    for item in loopback_capture:
        route_candidates.append({
            "kind": "windows_stereo_mix_capture",
            "capture_endpoint": brief_endpoint(item),
            "requires": "Set WeChat microphone/default recording device to this endpoint; play TTS on the normal speaker output.",
            "confidence": "candidate",
        })
    for item in virtual_capture:
        paired_render = best_virtual_render_pair(item, virtual_render)
        route_candidates.append({
            "kind": "virtual_audio_cable",
            "capture_endpoint": brief_endpoint(item),
            "render_endpoint": brief_endpoint(paired_render) if paired_render else {},
            "requires": "Play TTS to the paired virtual render endpoint and set WeChat microphone to the virtual capture endpoint.",
            "confidence": "candidate",
        })

    if not endpoints.get("ok"):
        status = "unsupported"
    elif route_candidates:
        status = "route_candidate_available"
    else:
        status = "needs_loopback_or_virtual_audio"

    content_ready = bool(route_candidates) and (
        not file_info.get("provided") or bool(file_info.get("valid"))
    )
    loopback_state = latest_loopback_diagnostic_state(audio_path)
    content_loopback_state = str(loopback_state.get("content_loopback_state") or "not_checked")
    default_capture = default_audio_endpoints("capture")
    default_capture_match = default_capture_route_match(default_capture, route_candidates)
    recommended_route_candidate = first_native_voice_capture_candidate({
        "route_candidates": route_candidates,
        "latest_loopback_diagnostic": loopback_state,
        "content_ready_proven": content_loopback_state == "passed",
    })
    next_action = native_voice_route_next_action(status, file_info, default_capture_match)
    if content_loopback_state == "passed":
        next_action = "Loopback diagnosis proved TTS content can be captured; native WeChat voice bubble sending can use the recommended route."
    return {
        "ok": True,
        "status": status,
        "content_ready_candidate": content_ready,
        "content_ready_proven": content_loopback_state == "passed",
        "audio_file": file_info,
        "route_candidates": route_candidates,
        "recommended_route_candidate": recommended_route_candidate,
        "default_capture_endpoints": default_capture,
        "default_capture_route_match": default_capture_match,
        "capture_ready_for_wechat": bool(content_ready and default_capture_match.get("all_roles_match")),
        "capture_partially_ready_for_wechat": bool(content_ready and default_capture_match.get("any_role_match")),
        "capture_endpoint_count": len(capture),
        "render_endpoint_count": len(render),
        "active_capture_endpoint_count": len(active_capture),
        "active_render_endpoint_count": len(active_render),
        "capture_endpoints": [brief_endpoint(item) for item in capture],
        "render_endpoints": [brief_endpoint(item) for item in render],
        "note": (
            "Native WeChat voice bubbles record from WeChat's selected microphone. "
            "A route candidate means the machine has a likely loopback/virtual endpoint, not that WeChat has selected it."
        ),
        "next_action": next_action,
        "content_loopback_state": content_loopback_state,
        "latest_loopback_diagnostic": loopback_state if content_loopback_state != "not_checked" else {},
    }


def native_voice_route_next_action(
    status: str,
    file_info: dict[str, Any],
    default_capture_match: dict[str, Any] | None = None,
) -> str:
    if file_info.get("provided") and not file_info.get("valid"):
        return "Use a valid non-empty WAV before testing native voice content."
    if status == "route_candidate_available":
        match = default_capture_match or {}
        if not match.get("any_role_match"):
            return "Set WeChat's microphone/default recording device to the recommended loopback or virtual capture endpoint, then retry a real voice bubble test."
        if not match.get("all_roles_match"):
            return "Some Windows recording roles already point at a candidate route; set all default recording roles or WeChat's microphone to that endpoint before a reliable voice bubble test."
        return "Default recording already points at a route candidate; run loopback diagnosis to prove TTS content is captured before enabling automatic native voice bubbles."
    if status == "needs_loopback_or_virtual_audio":
        return "Enable Stereo Mix if available or install/configure a virtual audio cable before expecting TTS content in WeChat voice bubbles."
    return "Windows endpoint inspection is unavailable on this host."


def latest_loopback_diagnostic_state(audio_path: str | Path | None = None) -> dict[str, Any]:
    for state_path in loopback_diagnostic_state_paths(audio_path):
        if not state_path.exists():
            continue
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {
                "content_loopback_state": "invalid",
                "state_file": str(state_path),
                "error": str(exc),
            }
        payload = dict(payload)
        payload["state_file"] = str(state_path)
        base_state = loopback_state_from_diagnostic_summary(payload)

        # The loopback proof expires after a TTL so a device reconfig/reboot can't
        # silently keep a stale "passed" alive. Override via env (seconds; <=0 off).
        try:
            ttl = float(os.environ.get("WECHAT_PC_VOICE_LOOPBACK_PROOF_TTL_SECONDS", "").strip() or 86400)
        except (TypeError, ValueError):
            ttl = 86400.0
        updated_at = payload.get("updated_at")
        if ttl > 0 and isinstance(updated_at, (int, float)) and (time.time() - float(updated_at)) > ttl:
            payload["content_loopback_state"] = "stale"
            payload["error"] = "loopback diagnostic proof has expired; re-run voice loopback diagnosis"
            payload["proof_age_seconds"] = round(time.time() - float(updated_at), 1)
            return payload

        requested = str(audio_path or "").strip()
        recorded = str(payload.get("audio_path") or "")
        audio_matches = True
        if requested and recorded:
            try:
                requested_resolved = str(Path(requested).resolve()).lower()
                recorded_resolved = str(Path(recorded).resolve()).lower()
            except Exception:
                requested_resolved = requested.lower()
                recorded_resolved = recorded.lower()
            audio_matches = requested_resolved == recorded_resolved

        if audio_matches:
            payload["content_loopback_state"] = base_state
            return payload

        # Different audio file: a *passed* loopback proves the capture/render route
        # can carry TTS content regardless of the specific clip, so reuse it as a
        # route-level proof instead of forcing a fresh diagnosis for every new file.
        if base_state == "passed":
            payload["content_loopback_state"] = "passed"
            payload["audio_file_mismatch"] = True
            payload["reused_as_route_proof"] = True
            return payload
        payload["content_loopback_state"] = "stale"
        payload["error"] = "latest loopback diagnostic was recorded for a different audio file and did not pass"
        return payload
    return {"content_loopback_state": "not_checked"}


def loopback_state_from_diagnostic_summary(payload: dict[str, Any]) -> str:
    if bool(payload.get("content_ready")) and str(payload.get("status") or "") == "content_route_passed":
        return "passed"
    status = str(payload.get("status") or "")
    if status in {"content_route_failed", "no_route_candidates", "invalid_audio_file"}:
        return "failed"
    return str(payload.get("content_loopback_state") or "not_checked")


def loopback_diagnostic_state_paths(
    audio_path: str | Path | None = None,
    *,
    output_dir: str | Path | None = None,
) -> list[Path]:
    source = Path(str(audio_path or "").strip()) if str(audio_path or "").strip() else None
    if output_dir:
        diagnostic_dirs = [Path(output_dir)]
    elif source is not None:
        diagnostic_dirs = [
            source.parent / "loopback-diagnostics",
            source.parent / "diagnostics",
        ]
    else:
        return []
    paths = []
    for diagnostic_dir in diagnostic_dirs:
        if source is not None and source.stem:
            paths.append(diagnostic_dir / f"{source.stem}.loopback-latest.json")
        paths.append(diagnostic_dir / LOOPBACK_DIAGNOSTIC_STATE_FILE)
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


def save_loopback_diagnostic_state(
    result: dict[str, Any],
    *,
    audio_path: str | Path,
    output_dir: str | Path,
) -> dict[str, Any]:
    payload = loopback_diagnostic_state_payload(result, audio_path=audio_path)
    saved: list[str] = []
    for state_path in loopback_diagnostic_state_paths(audio_path, output_dir=output_dir):
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        saved.append(str(state_path))
    return {"ok": True, "files": saved, "payload": payload}


def loopback_diagnostic_state_payload(result: dict[str, Any], *, audio_path: str | Path) -> dict[str, Any]:
    best = result.get("best_result") if isinstance(result.get("best_result"), dict) else {}
    return {
        "schema": "native_voice_loopback_diagnostic_v1",
        "updated_at": time.time(),
        "audio_path": str(Path(audio_path)),
        "status": str(result.get("status") or ""),
        "content_ready": bool(result.get("content_ready")),
        "content_loopback_state": loopback_state_from_diagnostic_summary(result),
        "attempt_count": int(result.get("attempt_count") or 0),
        "passed_count": int(result.get("passed_count") or 0),
        "output_dir": str(result.get("output_dir") or ""),
        "best_result": best,
        "route_status": result.get("route_status") if isinstance(result.get("route_status"), dict) else {},
        "next_action": str(result.get("next_action") or ""),
    }


def audio_file_info(path: str | Path | None) -> dict[str, Any]:
    raw_path = str(path or "").strip()
    if not raw_path:
        return {"provided": False, "valid": False}
    audio_path = Path(raw_path)
    info: dict[str, Any] = {
        "provided": True,
        "path": str(audio_path),
        "exists": audio_path.exists(),
        "valid": False,
    }
    if not audio_path.exists():
        info["error"] = "audio file does not exist"
        return info
    size = audio_path.stat().st_size
    info["bytes"] = size
    if audio_path.suffix.lower() != ".wav":
        info["error"] = "native voice recording playback currently supports wav audio"
        return info
    try:
        with wave.open(str(audio_path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
    except Exception as exc:
        info["error"] = str(exc)
        return info
    duration = frames / float(rate) if frames > 0 and rate > 0 else 0.0
    info.update({
        "frames": frames,
        "sample_rate": rate,
        "channels": channels,
        "sample_width": sample_width,
        "duration_seconds": round(duration, 3),
        "valid": size > 128 and frames > 0 and duration > 0,
    })
    if not info["valid"]:
        info["error"] = "audio file has no playable frames"
    return info


def endpoint_matches_keywords(endpoint: dict[str, Any], keywords: tuple[str, ...]) -> bool:
    text = endpoint_search_text(endpoint)
    return any(keyword.lower() in text for keyword in keywords)


def endpoint_search_text(endpoint: dict[str, Any]) -> str:
    fields = (
        "name",
        "friendly_name",
        "provider",
        "instance",
        "driver",
        "hardware",
        "portaudio_name",
        "portaudio_hostapi_name",
        "id",
        "full_id",
    )
    values = [str(endpoint.get(field) or "") for field in fields]
    raw_search_text = endpoint.get("search_text")
    if raw_search_text:
        values.append(str(raw_search_text))
    return " ".join(value for value in values if value).lower()


def brief_endpoint(endpoint: dict[str, Any] | None) -> dict[str, Any]:
    if not endpoint:
        return {}
    kind = str(endpoint.get("kind") or "")
    endpoint_id = str(endpoint.get("id") or "")
    result = {
        "id": endpoint_id,
        "full_id": str(endpoint.get("full_id") or mmdevice_full_id(endpoint_id, kind)),
        "name": str(endpoint.get("name") or ""),
        "friendly_name": str(endpoint.get("friendly_name") or ""),
        "provider": str(endpoint.get("provider") or ""),
        "kind": kind,
        "state": str(endpoint.get("state") or ""),
        "active": bool(endpoint.get("active")),
    }
    for key in (
        "portaudio_device_index",
        "portaudio_hostapi_name",
        "portaudio_name",
        "synthetic",
    ):
        if key in endpoint and endpoint.get(key) not in (None, ""):
            result[key] = endpoint.get(key)
    return result


def best_virtual_render_pair(
    capture_endpoint: dict[str, Any],
    render_endpoints: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not render_endpoints:
        return None
    capture_name = str(capture_endpoint.get("name") or "").lower()
    if "cable output" in capture_name:
        for item in render_endpoints:
            if "cable input" in str(item.get("name") or "").lower():
                return item
    if "voicemeeter output" in capture_name:
        for item in render_endpoints:
            if "voicemeeter input" in str(item.get("name") or "").lower():
                return item
    return render_endpoints[0]


def _read_mmdevices(kind: str) -> list[dict[str, Any]]:
    import winreg

    root_path = rf"{MMDEVICE_AUDIO_ROOT}\{kind}"
    endpoints: list[dict[str, Any]] = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root_path) as root_key:
        for index in range(winreg.QueryInfoKey(root_key)[0]):
            endpoint_id = winreg.EnumKey(root_key, index)
            endpoint_path = rf"{root_path}\{endpoint_id}"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, endpoint_path) as endpoint_key:
                state = _read_reg_value(endpoint_key, "DeviceState", 0)
            name = ""
            friendly_name = ""
            provider = ""
            instance = ""
            driver = ""
            hardware = ""
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"{endpoint_path}\Properties") as props:
                    name = str(_read_reg_value(props, MMDEVICE_NAME_PROP, "") or "")
                    friendly_name = str(_read_reg_value(props, MMDEVICE_FRIENDLY_NAME_PROP, "") or "")
                    provider = str(_read_reg_value(props, MMDEVICE_PROVIDER_PROP, "") or "")
                    instance = str(_read_reg_value(props, MMDEVICE_INSTANCE_PROP, "") or "")
                    driver = str(_read_reg_value(props, MMDEVICE_DRIVER_PROP, "") or "")
                    hardware = str(_read_reg_value(props, MMDEVICE_HARDWARE_PROP, "") or "")
            except OSError:
                name = ""
                friendly_name = ""
                provider = ""
                instance = ""
                driver = ""
                hardware = ""
            search_text = " ".join(
                value for value in (name, friendly_name, provider, instance, driver, hardware, endpoint_id)
                if value
            )
            endpoints.append({
                "id": endpoint_id,
                "full_id": mmdevice_full_id(endpoint_id, kind.lower()),
                "kind": kind.lower(),
                "name": name,
                "friendly_name": friendly_name,
                "provider": provider,
                "instance": instance,
                "driver": driver,
                "hardware": hardware,
                "search_text": search_text,
                "state_code": int(state or 0),
                "state": device_state_name(int(state or 0)),
                "active": device_state_is_active(int(state or 0)),
            })
    endpoints.sort(key=lambda item: (not bool(item.get("active")), str(item.get("name") or "").lower()))
    return endpoints


def _read_reg_value(key: Any, name: str, default: Any = None) -> Any:
    import winreg

    try:
        return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return default


def device_state_is_active(state: int) -> bool:
    return bool(int(state or 0) & 1)


def device_state_name(state: int) -> str:
    value = int(state or 0)
    if value in DEVICE_STATE_NAMES:
        return DEVICE_STATE_NAMES[value]
    names = [name for bit, name in DEVICE_STATE_BITS if value & bit]
    if not names:
        names.append(f"state_{value}")
    unknown_bits = value & ~sum(bit for bit, _name in DEVICE_STATE_BITS)
    if unknown_bits:
        names.append(f"raw_{unknown_bits}")
    return "+".join(names)


def mmdevice_full_id(endpoint_id: str, kind: str = "capture") -> str:
    raw = str(endpoint_id or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("{0.0."):
        return raw
    prefix = MMDEVICE_FULL_ID_PREFIX.get(str(kind or "").lower(), MMDEVICE_FULL_ID_PREFIX["capture"])
    return f"{prefix}.{raw}"


def mmdevice_short_id(endpoint_id: str) -> str:
    raw = str(endpoint_id or "").strip()
    if "}.{" in raw:
        return raw.rsplit(".", 1)[-1]
    return raw


def endpoint_identity_matches(observed: str, expected: str, *, kind: str = "capture") -> bool:
    observed_raw = str(observed or "").strip().lower()
    expected_raw = str(expected or "").strip().lower()
    if not observed_raw or not expected_raw:
        return False
    observed_values = {
        observed_raw,
        mmdevice_short_id(observed_raw).lower(),
        mmdevice_full_id(observed_raw, kind).lower(),
    }
    expected_values = {
        expected_raw,
        mmdevice_short_id(expected_raw).lower(),
        mmdevice_full_id(expected_raw, kind).lower(),
    }
    return bool(observed_values & expected_values)


def find_audio_endpoint(kind: str, endpoint_id: str) -> dict[str, Any]:
    endpoints = enumerate_audio_endpoints()
    items = list(endpoints.get(str(kind or "").lower()) or [])
    for item in items:
        if endpoint_identity_matches(str(item.get("id") or ""), endpoint_id, kind=kind):
            return dict(item)
        if endpoint_identity_matches(str(item.get("full_id") or ""), endpoint_id, kind=kind):
            return dict(item)
    return {}


def default_audio_endpoint(kind: str = "capture", role: str = "console") -> dict[str, Any]:
    if os.name != "nt":
        return {"ok": False, "kind": kind, "role": role, "error": "Windows Core Audio is required"}
    try:
        role_index = audio_role_index(role)
        flow_index = audio_flow_index(kind)
        enumerator, _policy_class = _core_audio_objects()
        device = enumerator.GetDefaultAudioEndpoint(flow_index, role_index)
        full_id = str(device.GetId() or "")
        short_id = mmdevice_short_id(full_id)
        state_code = int(device.GetState())
        endpoint = find_audio_endpoint(kind, full_id)
        return {
            "ok": True,
            "kind": kind,
            "role": role,
            "role_index": role_index,
            "id": short_id,
            "full_id": full_id,
            "state_code": state_code,
            "state": device_state_name(state_code),
            "active": device_state_is_active(state_code),
            "endpoint": brief_endpoint(endpoint),
        }
    except Exception as exc:
        return {"ok": False, "kind": kind, "role": role, "error": str(exc)}


def default_audio_endpoints(kind: str = "capture") -> dict[str, Any]:
    roles = {role: default_audio_endpoint(kind, role) for role in DEFAULT_AUDIO_ROLES}
    return {
        "ok": all(item.get("ok") for item in roles.values()),
        "kind": kind,
        "roles": roles,
    }


def default_capture_route_match(
    default_capture: dict[str, Any],
    route_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    roles = dict(default_capture.get("roles") or {})
    role_results: list[dict[str, Any]] = []
    matched_roles: list[str] = []
    unmatched_roles: list[str] = []
    candidate_captures = [
        (index, dict(candidate), dict(candidate.get("capture_endpoint") or {}))
        for index, candidate in enumerate(route_candidates)
        if isinstance(candidate, dict) and isinstance(candidate.get("capture_endpoint"), dict)
    ]

    for role_name, raw_role in roles.items():
        role = dict(raw_role) if isinstance(raw_role, dict) else {}
        matched_candidate: dict[str, Any] = {}
        matched_index: int | None = None
        for index, candidate, capture in candidate_captures:
            if default_capture_role_matches_candidate(role, capture):
                matched_candidate = candidate
                matched_index = index
                break
        matched = matched_index is not None
        if matched:
            matched_roles.append(str(role_name))
        else:
            unmatched_roles.append(str(role_name))
        role_results.append({
            "role": str(role_name),
            "ok": bool(role.get("ok")),
            "default_endpoint": default_capture_role_brief_endpoint(role),
            "matches_route_candidate": matched,
            "matched_candidate_index": matched_index,
            "matched_candidate_kind": str(matched_candidate.get("kind") or "") if matched else "",
        })

    role_count = len(role_results)
    matched_count = len(matched_roles)
    return {
        "ok": bool(default_capture.get("ok")) if "ok" in default_capture else bool(role_results),
        "kind": "capture",
        "role_count": role_count,
        "matched_count": matched_count,
        "any_role_match": matched_count > 0,
        "all_roles_match": role_count > 0 and matched_count == role_count,
        "matched_roles": matched_roles,
        "unmatched_roles": unmatched_roles,
        "roles": role_results,
    }


def default_capture_role_matches_candidate(role: dict[str, Any], capture_endpoint: dict[str, Any]) -> bool:
    observed_values = [
        str(role.get("full_id") or ""),
        str(role.get("id") or ""),
    ]
    role_endpoint = role.get("endpoint") if isinstance(role.get("endpoint"), dict) else {}
    observed_values.extend([
        str(role_endpoint.get("full_id") or ""),
        str(role_endpoint.get("id") or ""),
    ])
    expected_values = [
        str(capture_endpoint.get("full_id") or ""),
        str(capture_endpoint.get("id") or ""),
    ]
    for observed in observed_values:
        for expected in expected_values:
            if endpoint_identity_matches(observed, expected, kind="capture"):
                return True
    return False


def default_capture_role_brief_endpoint(role: dict[str, Any]) -> dict[str, Any]:
    endpoint = role.get("endpoint") if isinstance(role.get("endpoint"), dict) else {}
    if endpoint:
        return dict(endpoint)
    return {
        "id": str(role.get("id") or ""),
        "full_id": str(role.get("full_id") or ""),
        "name": "",
        "friendly_name": "",
        "provider": "",
        "kind": str(role.get("kind") or "capture"),
        "state": str(role.get("state") or ""),
        "active": bool(role.get("active")),
    }


def set_default_audio_endpoint(
    endpoint_id: str,
    *,
    kind: str = "capture",
    roles: Iterable[str] | None = None,
) -> dict[str, Any]:
    role_names = normalize_audio_roles(roles)
    target_full_id = mmdevice_full_id(endpoint_id, kind)
    target = find_audio_endpoint(kind, target_full_id)
    before = default_audio_endpoints(kind)
    result: dict[str, Any] = {
        "ok": False,
        "kind": kind,
        "target_id": mmdevice_short_id(target_full_id),
        "target_full_id": target_full_id,
        "target_endpoint": brief_endpoint(target),
        "roles": role_names,
        "before": before,
        "set_results": [],
    }
    if os.name != "nt":
        result["error"] = "Windows Core Audio is required"
        return result
    if not target_full_id:
        result["error"] = "target endpoint id is empty"
        return result
    if not target:
        result["error"] = "target endpoint was not found"
        return result
    core_state = core_audio_endpoint_state(target_full_id, kind=kind)
    result["core_audio_state_before"] = core_state
    if core_state.get("ok") and not core_state.get("active"):
        visibility = set_audio_endpoint_visibility(target_full_id, visible=True, kind=kind)
        result["visibility"] = visibility
        core_state = dict(visibility.get("after") or core_audio_endpoint_state(target_full_id, kind=kind))
    result["core_audio_state_ready"] = core_state
    if core_state.get("ok") and not core_state.get("active"):
        result["error"] = "target endpoint is not active"
        return result
    if target and not target.get("active") and not core_state.get("active"):
        result["error"] = "target endpoint is not active"
        return result
    errors: list[str] = []
    try:
        _enumerator, policy = _core_audio_objects()
        for role_name in role_names:
            role_index = audio_role_index(role_name)
            try:
                policy.SetDefaultEndpoint(target_full_id, role_index)
                result["set_results"].append({"role": role_name, "ok": True})
            except Exception as exc:
                message = str(exc)
                errors.append(f"{role_name}: {message}")
                result["set_results"].append({"role": role_name, "ok": False, "error": message})
    except Exception as exc:
        result["error"] = str(exc)
        return result
    after = default_audio_endpoints(kind)
    result["after"] = after
    verified = []
    for role_name in role_names:
        observed = ((after.get("roles") or {}).get(role_name) or {}).get("full_id", "")
        verified.append(endpoint_identity_matches(str(observed), target_full_id, kind=kind))
    result["verified"] = all(verified) if verified else False
    if errors:
        result["error"] = "; ".join(errors)
    result["ok"] = not errors and bool(result["verified"])
    return result


def begin_native_voice_audio_route(
    audio_path: str | Path | None = None,
    *,
    require_content_proven: bool = False,
) -> dict[str, Any]:
    route_status = native_voice_route_status(audio_path)
    result: dict[str, Any] = {
        "ok": False,
        "attempted": False,
        "require_content_proven": bool(require_content_proven),
        "route_status": route_status,
        "previous_defaults": default_audio_endpoints("capture"),
        "previous_render_defaults": default_audio_endpoints("render"),
    }
    if not route_status.get("content_ready_candidate"):
        result["error"] = route_status.get("next_action") or "native voice audio route is not ready"
        return result
    # 真正的虚拟声卡(VB-CABLE/VoiceMeeter)Input→Output 必互通，但 sd.playrec 诊断对其采样率挑剔(-9997)；
    # 真发走 winsound 共享模式自动重采样不受影响。允许用该 env 跳过 playrec 硬证明(操作者确认过虚拟声卡可用)。
    env_skip = os.environ.get("WECHAT_PC_VOICE_SKIP_LOOPBACK_PROOF", "").strip().lower() in {"1", "true", "yes", "on"}
    # 有可信虚拟声卡(VB-CABLE/VoiceMeeter)时自动跳过 playrec 硬证明：它 Input→Output 必互通，
    # 但 sd.playrec 诊断对其采样率挑剔(-9997)；真发走 portaudio 专播不受影响。
    has_trusted_cable = any(
        c.get("kind") == "virtual_audio_cable"
        and (c.get("render_endpoint") or {})
        and not candidate_is_untrusted_virtual_audio(c)
        for c in (route_status.get("route_candidates") or [])
    )
    skip_proof = env_skip or has_trusted_cable
    if require_content_proven and not skip_proof and not route_status.get("content_ready_proven"):
        latest = route_status.get("latest_loopback_diagnostic") or {}
        state = route_status.get("content_loopback_state") or "not_checked"
        result["error"] = (
            "native voice content route is not proven by loopback diagnostic "
            f"(state={state}); run voice loopback diagnosis before sending TTS as a native WeChat voice bubble"
        )
        result["latest_loopback_diagnostic"] = latest
        return result
    candidate = first_native_voice_capture_candidate(route_status)
    capture = dict(candidate.get("capture_endpoint") or {})
    render = dict(candidate.get("render_endpoint") or {})
    capture_id = str(capture.get("full_id") or capture.get("id") or "")
    render_id = "" if endpoint_portaudio_device_index(render) is not None else str(render.get("full_id") or render.get("id") or "")
    capture_device_index = endpoint_portaudio_device_index(capture)
    render_device_index = endpoint_portaudio_device_index(render)
    if not capture_id:
        result["error"] = "route candidate has no capture endpoint id"
        return result
    # stereo-mix 候选默认不带 render：把默认输出临时切到与 Stereo Mix 同一声卡(Realtek)的渲染端点，
    # 这样 winsound 播到默认输出→Realtek→Stereo Mix 采得到→微信录得进；发完由 restore 还原回(如)耳机。
    if not render_id and str(candidate.get("kind") or "") == "windows_stereo_mix_capture":
        cap_provider = str(capture.get("provider") or "").strip().lower()
        try:
            render_list = list(enumerate_audio_endpoints().get("render") or [])
        except Exception:
            render_list = []
        for item in render_list:
            if not item.get("active"):
                continue
            if cap_provider and cap_provider in str(item.get("provider") or "").strip().lower():
                render = dict(item)
                render_id = str(item.get("full_id") or item.get("id") or "")
                render_device_index = endpoint_portaudio_device_index(render)
                break
    # 虚拟声卡(VB-CABLE)：绝不切默认输出！否则系统其它声音(抖音/视频/提示音)也会灌进 cable 被微信一起录进去。
    # 改为保持默认输出不变(系统声继续走耳机)，只把 TTS 用专用 portaudio 流单独播到 cable 输入端
    # (selected_render_endpoint 已提供给播放层，配合 WECHAT_PC_VOICE_PLAYBACK=portaudio)。
    if str(candidate.get("kind") or "") == "virtual_audio_cable":
        render_id = ""
    set_render_result: dict[str, Any] = {}
    if render_id:
        set_render_result = set_default_audio_endpoint(render_id, kind="render", roles=DEFAULT_AUDIO_ROLES)
    set_result = set_default_audio_endpoint(capture_id, kind="capture", roles=DEFAULT_AUDIO_ROLES)
    route_ok = bool(set_result.get("ok")) and (not render_id or bool(set_render_result.get("ok")))
    result.update({
        "attempted": True,
        "candidate": candidate,
        "selected_capture_endpoint": capture,
        "selected_render_endpoint": render,
        "selected_capture_device_index": capture_device_index,
        "selected_render_device_index": render_device_index,
        "set_default_capture": set_result,
        "set_default_render": set_render_result,
        "ok": route_ok,
    })
    if not result["ok"]:
        result["error"] = (
            set_render_result.get("error")
            or set_result.get("error")
            or "failed to switch default audio route"
        )
    return result


def restore_native_voice_audio_route(route_session: dict[str, Any] | None) -> dict[str, Any]:
    if not route_session:
        return {"ok": True, "restored": False, "reason": "no route session"}
    previous_capture = dict(route_session.get("previous_defaults") or {})
    previous_render = dict(route_session.get("previous_render_defaults") or {})
    result: dict[str, Any] = {
        "ok": False,
        "restored": False,
        "roles": [],
        "capture_roles": [],
        "render_roles": [],
        "errors": [],
        "previous_defaults": previous_capture,
        "previous_render_defaults": previous_render,
    }
    if os.name != "nt":
        result["error"] = "Windows Core Audio is required"
        return result
    try:
        _enumerator, policy = _core_audio_objects()
        _restore_previous_default_roles(policy, previous_render, "render", result["render_roles"], result["errors"])
        _restore_previous_default_roles(policy, previous_capture, "capture", result["capture_roles"], result["errors"])
    except Exception as exc:
        result["error"] = str(exc)
        return result
    capture_after = default_audio_endpoints("capture")
    render_after = default_audio_endpoints("render")
    result["after"] = capture_after
    result["capture_after"] = capture_after
    result["render_after"] = render_after
    capture_verified = _verify_previous_default_roles(previous_capture, capture_after, "capture")
    render_verified = _verify_previous_default_roles(previous_render, render_after, "render")
    result["roles"] = list(result["capture_roles"])
    result["restored"] = bool(result["capture_roles"] or result["render_roles"])
    result["capture_verified"] = capture_verified
    result["render_verified"] = render_verified
    result["verified"] = bool(capture_verified and render_verified)
    result["ok"] = not result["errors"] and bool(result["verified"])
    if result["errors"]:
        result["error"] = "; ".join(result["errors"])
    visibility = (((route_session.get("set_default_capture") or {}).get("visibility")) or {})
    before_visibility = dict(visibility.get("before") or {})
    after_visibility = dict(visibility.get("after") or {})
    if (
        visibility.get("ok")
        and before_visibility.get("ok")
        and after_visibility.get("ok")
        and not before_visibility.get("active")
        and after_visibility.get("active")
    ):
        target_full_id = str(after_visibility.get("full_id") or "")
        result["visibility_restore"] = set_audio_endpoint_visibility(target_full_id, visible=False, kind="capture")
    return result


def first_native_voice_capture_candidate(route_status: dict[str, Any]) -> dict[str, Any]:
    proven = proven_loopback_route_candidate(route_status)
    if proven:
        return proven

    candidates = [dict(candidate) for candidate in list(route_status.get("route_candidates") or [])]
    trusted_virtual = [
        candidate for candidate in candidates
        if (
            candidate.get("kind") == "virtual_audio_cable"
            and (candidate.get("render_endpoint") or {})
            and not candidate_is_untrusted_virtual_audio(candidate)
        )
    ]
    stereo_mix = [
        candidate for candidate in candidates
        if candidate.get("kind") == "windows_stereo_mix_capture"
    ]
    virtual_with_render = [
        candidate for candidate in candidates
        if candidate.get("kind") == "virtual_audio_cable" and (candidate.get("render_endpoint") or {})
    ]
    for candidate in trusted_virtual + stereo_mix + virtual_with_render + candidates:
        capture = candidate.get("capture_endpoint") or {}
        if capture.get("id") or capture.get("full_id"):
            return dict(candidate)
    return {}


def proven_loopback_route_candidate(route_status: dict[str, Any]) -> dict[str, Any]:
    if not route_status.get("content_ready_proven"):
        return {}
    state = route_status.get("latest_loopback_diagnostic")
    if not isinstance(state, dict) or loopback_state_from_diagnostic_summary(state) != "passed":
        return {}
    best = state.get("best_result")
    if not isinstance(best, dict):
        return {}
    verdict = best.get("verdict") if isinstance(best.get("verdict"), dict) else {}
    if verdict and not bool(verdict.get("passed")):
        return {}
    attempt = best.get("attempt") if isinstance(best.get("attempt"), dict) else {}
    candidate = dict(attempt.get("candidate") or {}) if isinstance(attempt.get("candidate"), dict) else {}
    if not candidate:
        candidate = {"kind": str(attempt.get("kind") or "")}
    capture = dict(candidate.get("capture_endpoint") or {})
    render = dict(candidate.get("render_endpoint") or {})
    if isinstance(attempt.get("capture_endpoint"), dict):
        capture.update(dict(attempt.get("capture_endpoint") or {}))
    if isinstance(attempt.get("render_endpoint"), dict):
        render.update(dict(attempt.get("render_endpoint") or {}))
    if not (capture.get("id") or capture.get("full_id")):
        return {}
    capture_index = endpoint_portaudio_device_index(capture)
    render_index = endpoint_portaudio_device_index(render)
    if capture_index is None and best.get("capture_device_index") not in (None, ""):
        capture["portaudio_device_index"] = int(best.get("capture_device_index"))
        capture_index = int(best.get("capture_device_index"))
    if render_index is None and best.get("render_device_index") not in (None, ""):
        render["portaudio_device_index"] = int(best.get("render_device_index"))
        render_index = int(best.get("render_device_index"))
    candidate["capture_endpoint"] = brief_endpoint(capture)
    if render:
        candidate["render_endpoint"] = brief_endpoint(render)
    candidate["confidence"] = "proven_loopback"
    candidate["loopback_state_file"] = str(state.get("state_file") or "")
    candidate["loopback_output_path"] = str(best.get("output_path") or "")
    candidate["diagnostic_correlation"] = float(best.get("correlation") or verdict.get("correlation") or 0.0)
    if capture_index is not None:
        candidate["diagnostic_capture_device_index"] = capture_index
    if render_index is not None:
        candidate["diagnostic_render_device_index"] = render_index
    return candidate


def candidate_is_untrusted_virtual_audio(candidate: dict[str, Any]) -> bool:
    capture = candidate.get("capture_endpoint") if isinstance(candidate.get("capture_endpoint"), dict) else {}
    render = candidate.get("render_endpoint") if isinstance(candidate.get("render_endpoint"), dict) else {}
    return bool(
        endpoint_matches_keywords(capture, UNTRUSTED_VIRTUAL_CAPTURE_KEYWORDS)
        or endpoint_matches_keywords(render, UNTRUSTED_VIRTUAL_CAPTURE_KEYWORDS)
    )


def list_portaudio_devices() -> dict[str, Any]:
    try:
        sd = _import_sounddevice()
        hostapis = list(sd.query_hostapis())
        devices = []
        for index, raw in enumerate(sd.query_devices()):
            hostapi_index = int(raw.get("hostapi") or 0)
            hostapi_name = _hostapi_name(hostapis, hostapi_index)
            devices.append({
                "index": index,
                "name": str(raw.get("name") or ""),
                "hostapi": hostapi_index,
                "hostapi_name": hostapi_name,
                "max_input_channels": int(raw.get("max_input_channels") or 0),
                "max_output_channels": int(raw.get("max_output_channels") or 0),
                "default_samplerate": float(raw.get("default_samplerate") or 0.0),
            })
        return {
            "ok": True,
            "devices": devices,
            "hostapis": [
                {"index": index, "name": str(item.get("name") or "")}
                for index, item in enumerate(hostapis)
            ],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc), "devices": [], "hostapis": []}


def select_portaudio_device(kind: str, endpoint: dict[str, Any] | None = None) -> dict[str, Any]:
    listing = list_portaudio_devices()
    if not listing.get("ok"):
        return {"ok": False, "kind": kind, "error": listing.get("error") or "PortAudio device listing failed"}
    selected = select_portaudio_device_from_devices(
        list(listing.get("devices") or []),
        kind,
        endpoint or {},
    )
    if not selected.get("ok"):
        selected["portaudio"] = listing
    return selected


def select_portaudio_device_from_devices(
    devices: list[dict[str, Any]],
    kind: str,
    endpoint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_kind = str(kind or "").strip().lower()
    endpoint = endpoint or {}
    explicit_index = endpoint_portaudio_device_index(endpoint)
    if explicit_index is not None:
        for device in devices:
            if int(device.get("index") or -1) != explicit_index:
                continue
            if normalized_kind == "capture" and int(device.get("max_input_channels") or 0) <= 0:
                break
            if normalized_kind == "render" and int(device.get("max_output_channels") or 0) <= 0:
                break
            return {
                "ok": True,
                "kind": normalized_kind,
                "score": 10000,
                "endpoint": brief_endpoint(endpoint),
                "device": dict(device),
                "device_index": explicit_index,
                "selection_source": "explicit_portaudio_device_index",
            }
        return {
            "ok": False,
            "kind": normalized_kind,
            "endpoint": brief_endpoint(endpoint),
            "device_index": explicit_index,
            "error": "explicit PortAudio device index was not found or does not support the requested direction",
        }
    scored: list[tuple[int, dict[str, Any]]] = []
    for device in devices:
        if normalized_kind == "capture" and int(device.get("max_input_channels") or 0) <= 0:
            continue
        if normalized_kind == "render" and int(device.get("max_output_channels") or 0) <= 0:
            continue
        score = portaudio_device_match_score(device, normalized_kind, endpoint)
        if score > 0:
            scored.append((score, dict(device)))
    if not scored:
        return {
            "ok": False,
            "kind": normalized_kind,
            "endpoint": brief_endpoint(endpoint),
            "error": "no matching PortAudio device was found",
        }
    scored.sort(key=lambda item: (-item[0], int(item[1].get("index") or 0)))
    score, device = scored[0]
    return {
        "ok": True,
        "kind": normalized_kind,
        "score": score,
        "endpoint": brief_endpoint(endpoint),
        "device": device,
        "device_index": int(device.get("index") or 0),
    }


def portaudio_device_match_score(device: dict[str, Any], kind: str, endpoint: dict[str, Any] | None = None) -> int:
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind == "capture" and int(device.get("max_input_channels") or 0) <= 0:
        return 0
    if normalized_kind == "render" and int(device.get("max_output_channels") or 0) <= 0:
        return 0
    endpoint = endpoint or {}
    endpoint_text = endpoint_search_text(endpoint)
    device_name = str(device.get("name") or "").lower()
    host_name = str(device.get("hostapi_name") or "")
    score = PORTAUDIO_HOST_PRIORITY.get(host_name, 1)
    exact_names = [
        str(endpoint.get("name") or "").strip().lower(),
        str(endpoint.get("friendly_name") or "").strip().lower(),
    ]
    if not endpoint_text.strip():
        return score
    if any(name and name in device_name for name in exact_names):
        score += 100
    tokens = [
        token
        for token in _split_endpoint_tokens(endpoint_text)
        if len(token) >= 3 and token not in {"audio", "high", "definition", "endpoint", "root"}
    ]
    if tokens:
        matched = sum(1 for token in tokens if token in device_name)
        score += matched * 10
        if matched == 0:
            return 0
    return score


def endpoint_portaudio_device_index(endpoint: dict[str, Any] | None) -> int | None:
    if not isinstance(endpoint, dict):
        return None
    for key in ("portaudio_device_index", "device_index", "diagnostic_device_index"):
        if key not in endpoint or endpoint.get(key) in (None, ""):
            continue
        try:
            value = int(endpoint.get(key))
        except (TypeError, ValueError):
            continue
        if value >= 0:
            return value
    return None


def play_wav_to_render_endpoint(path: str | Path, render_endpoint: dict[str, Any]) -> dict[str, Any]:
    selected = select_portaudio_device("render", render_endpoint)
    if not selected.get("ok"):
        return {
            "started": False,
            "method": "sounddevice_endpoint",
            "path": str(path),
            "selection": selected,
            "error": selected.get("error") or "render endpoint was not selectable",
        }
    return play_wav_to_portaudio_device(path, int(selected.get("device_index") or 0), selected=selected)


def prepare_wav_for_portaudio_device(
    path: str | Path,
    device_index: int,
    *,
    selected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read + resample a WAV for a PortAudio device WITHOUT starting playback.

    This is the length-proportional work (decode + resample); doing it before the
    WeChat recording starts keeps the leading silence in the voice bubble minimal.
    """
    try:
        sd = _import_sounddevice()
        np = _import_numpy()
        rate, data = _read_wav_float32(path)
        device = dict(sd.query_devices(int(device_index)))
        playback_rate = int(device.get("default_samplerate") or rate)
        playback = _prepare_playback_array(
            data,
            source_rate=rate,
            playback_rate=playback_rate,
            output_channels=max(1, min(2, int(device.get("max_output_channels") or 1))),
        )
        array = np.asarray(playback, dtype=np.float32)
        return {
            "ok": True,
            "array": array,
            "device_index": int(device_index),
            "playback_sample_rate": playback_rate,
            "source_sample_rate": rate,
            "channels": int(array.shape[1]) if len(array.shape) > 1 else 1,
            "duration_seconds": round(float(array.shape[0]) / float(playback_rate), 3),
            "device": {
                "name": str(device.get("name") or ""),
                "max_output_channels": int(device.get("max_output_channels") or 0),
                "default_samplerate": float(device.get("default_samplerate") or 0.0),
            },
            "selection": selected or {},
            "path": str(path),
        }
    except Exception as exc:
        return {
            "ok": False,
            "device_index": int(device_index),
            "selection": selected or {},
            "path": str(path),
            "error": str(exc),
        }


def prewarm_portaudio_device(prepared: dict[str, Any], *, seconds: float = 0.25) -> dict[str, Any]:
    """Open/exercise the output stream with a short silence so the subsequent real
    playback reuses a warm stream and starts almost immediately. Call before the
    WeChat recording begins (the silence is harmless and inaudible)."""
    try:
        sd = _import_sounddevice()
        np = _import_numpy()
        rate = int(prepared.get("playback_sample_rate") or 0)
        channels = int(prepared.get("channels") or 1)
        device_index = int(prepared.get("device_index"))
        if rate <= 0:
            return {"warmed": False, "error": "missing playback sample rate"}
        frames = max(1, int(rate * max(0.05, seconds)))
        silence = np.zeros((frames, channels), dtype=np.float32)
        sd.play(silence, samplerate=rate, device=device_index, blocking=True)
        return {"warmed": True, "device_index": device_index, "seconds": round(frames / rate, 3)}
    except Exception as exc:
        return {"warmed": False, "error": str(exc)}


def play_prepared_portaudio(prepared: dict[str, Any]) -> dict[str, Any]:
    """Trigger playback of an already-prepared (and ideally prewarmed) buffer.

    This is the fast path — no decode/resample — so it should be called right after
    the WeChat recording starts."""
    try:
        sd = _import_sounddevice()
        sd.play(
            prepared["array"],
            samplerate=int(prepared["playback_sample_rate"]),
            device=int(prepared["device_index"]),
            blocking=False,
        )
        return {
            "started": True,
            "method": "sounddevice_endpoint_prepared",
            "path": prepared.get("path"),
            "device_index": int(prepared["device_index"]),
            "device": prepared.get("device", {}),
            "selection": prepared.get("selection", {}),
            "source_sample_rate": prepared.get("source_sample_rate"),
            "playback_sample_rate": prepared.get("playback_sample_rate"),
            "duration_seconds": prepared.get("duration_seconds"),
            "channels": prepared.get("channels"),
        }
    except Exception as exc:
        return {
            "started": False,
            "method": "sounddevice_endpoint_prepared",
            "path": prepared.get("path"),
            "device_index": prepared.get("device_index"),
            "error": str(exc),
        }


def play_wav_to_portaudio_device(
    path: str | Path,
    device_index: int,
    *,
    selected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prepared = prepare_wav_for_portaudio_device(path, device_index, selected=selected)
    if not prepared.get("ok"):
        return {
            "started": False,
            "method": "sounddevice_endpoint",
            "path": str(path),
            "device_index": int(device_index),
            "selection": selected or {},
            "error": prepared.get("error") or "playback preparation failed",
        }
    result = play_prepared_portaudio(prepared)
    result.setdefault("method", "sounddevice_endpoint")
    result["path"] = str(path)
    return result


def stop_sounddevice_playback() -> dict[str, Any]:
    try:
        sd = _import_sounddevice()
        sd.stop()
        return {"stopped": True, "method": "sounddevice_stop"}
    except Exception as exc:
        return {"stopped": False, "method": "sounddevice_stop", "error": str(exc)}


def diagnose_native_voice_loopback(
    audio_path: str | Path,
    *,
    output_path: str | Path | None = None,
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    file_info = audio_file_info(audio_path)
    if not file_info.get("valid"):
        return {
            "ok": False,
            "audio_file": file_info,
            "error": file_info.get("error") or "audio file is not playable",
        }
    route_status = native_voice_route_status(audio_path)
    selected_candidate = dict(candidate or first_native_voice_capture_candidate(route_status))
    capture_endpoint = dict(selected_candidate.get("capture_endpoint") or {})
    render_endpoint = dict(selected_candidate.get("render_endpoint") or {})
    if not capture_endpoint:
        return {
            "ok": False,
            "audio_file": file_info,
            "route_status": route_status,
            "error": "no capture route candidate is available",
        }
    if not render_endpoint:
        default_render = default_audio_endpoint("render", "multimedia")
        render_endpoint = dict(default_render.get("endpoint") or {})
    capture_selection = select_portaudio_device("capture", capture_endpoint)
    render_selection = select_portaudio_device("render", render_endpoint)
    if not capture_selection.get("ok") or not render_selection.get("ok"):
        return {
            "ok": False,
            "audio_file": file_info,
            "route_status": route_status,
            "candidate": selected_candidate,
            "capture_selection": capture_selection,
            "render_selection": render_selection,
            "error": "PortAudio input/output device selection failed",
        }
    try:
        result = _run_loopback_diagnostic(
            audio_path,
            int(capture_selection.get("device_index") or 0),
            int(render_selection.get("device_index") or 0),
            output_path=output_path,
        )
    except Exception as exc:
        return {
            "ok": False,
            "audio_file": file_info,
            "route_status": route_status,
            "candidate": selected_candidate,
            "capture_selection": capture_selection,
            "render_selection": render_selection,
            "error": str(exc),
        }
    result.update({
        "route_status": route_status,
        "candidate": selected_candidate,
        "capture_selection": capture_selection,
        "render_selection": render_selection,
    })
    return result


def diagnose_all_native_voice_loopbacks(
    audio_path: str | Path,
    *,
    output_dir: str | Path | None = None,
    max_attempts: int = 12,
) -> dict[str, Any]:
    file_info = audio_file_info(audio_path)
    route_status = native_voice_route_status(audio_path)
    if not file_info.get("valid"):
        return {
            "ok": False,
            "status": "invalid_audio_file",
            "content_ready": False,
            "audio_file": file_info,
            "route_status": compact_native_voice_route_status(route_status),
            "error": file_info.get("error") or "audio file is not playable",
        }

    attempts = native_voice_loopback_diagnostic_attempts(
        route_status,
        max_attempts=max_attempts,
    )
    if not attempts:
        return {
            "ok": False,
            "status": "no_route_candidates",
            "content_ready": False,
            "audio_file": file_info,
            "route_status": compact_native_voice_route_status(route_status),
            "attempt_count": 0,
            "results": [],
            "error": "no capture/render route candidates are available",
            "next_action": "Install/configure a real virtual audio cable or enable a working Stereo Mix route.",
        }

    diagnostic_dir = Path(output_dir) if output_dir else Path(audio_path).parent / "loopback-diagnostics"
    diagnostic_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for index, attempt in enumerate(attempts, start=1):
        output_path = diagnostic_dir / f"{index:02d}-{_diagnostic_attempt_slug(attempt)}.wav"
        candidate = dict(attempt.get("candidate") or {})
        result = diagnose_native_voice_loopback(
            audio_path,
            output_path=output_path,
            candidate=candidate,
        )
        results.append(compact_loopback_diagnostic_result(result, attempt=attempt))

    passed_results = [
        item for item in results
        if bool(((item.get("verdict") or {}) if isinstance(item.get("verdict"), dict) else {}).get("passed"))
    ]
    best_result = passed_results[0] if passed_results else best_loopback_diagnostic_result(results)
    content_ready = bool(passed_results)
    if content_ready:
        status = "content_route_passed"
        next_action = "Use the passing capture/render pair for native WeChat voice bubble tests."
    else:
        status = "content_route_failed"
        next_action = "Install/configure a real virtual audio cable such as VB-CABLE/Voicemeeter, then rerun this loopback diagnostic."
    result = {
        "ok": True,
        "status": status,
        "content_ready": content_ready,
        "audio_file": file_info,
        "route_status": compact_native_voice_route_status(route_status),
        "output_dir": str(diagnostic_dir),
        "attempt_count": len(results),
        "passed_count": len(passed_results),
        "best_result": best_result,
        "results": results,
        "next_action": next_action,
    }
    try:
        result["state_save"] = save_loopback_diagnostic_state(
            result,
            audio_path=audio_path,
            output_dir=diagnostic_dir,
        )
    except Exception as exc:
        result["state_save"] = {"ok": False, "error": str(exc)}
    return result


def native_voice_loopback_diagnostic_attempts(
    route_status: dict[str, Any],
    *,
    max_attempts: int = 12,
) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for candidate in list(route_status.get("route_candidates") or []):
        for capture_endpoint in capture_endpoints_for_diagnostic_candidate(candidate):
            if not capture_endpoint:
                continue
            candidate_with_capture = dict(candidate)
            candidate_with_capture["capture_endpoint"] = capture_endpoint
            for render_endpoint in render_endpoints_for_diagnostic_candidate(route_status, candidate_with_capture):
                render_endpoint = dict(render_endpoint or {})
                if not render_endpoint:
                    continue
                candidate_with_render = dict(candidate_with_capture)
                candidate_with_render["render_endpoint"] = render_endpoint
                key = (
                    str(candidate_with_render.get("kind") or ""),
                    endpoint_identity(capture_endpoint),
                    endpoint_identity(render_endpoint),
                )
                if key in seen:
                    continue
                seen.add(key)
                attempts.append({
                    "index": len(attempts) + 1,
                    "kind": str(candidate_with_render.get("kind") or ""),
                    "candidate": candidate_with_render,
                    "capture_endpoint": capture_endpoint,
                    "render_endpoint": render_endpoint,
                })
                if len(attempts) >= max(1, int(max_attempts or 1)):
                    return attempts
    return attempts


def capture_endpoints_for_diagnostic_candidate(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    capture_endpoint = dict(candidate.get("capture_endpoint") or {})
    if not capture_endpoint:
        return []
    endpoints: list[dict[str, Any]] = []
    if candidate.get("kind") == "windows_stereo_mix_capture":
        endpoints.extend(portaudio_stereo_mix_capture_candidates(capture_endpoint))
    endpoints.append(capture_endpoint)
    return dedupe_endpoints(endpoints)


def render_endpoints_for_diagnostic_candidate(
    route_status: dict[str, Any],
    candidate: dict[str, Any],
) -> list[dict[str, Any]]:
    render_endpoint = dict(candidate.get("render_endpoint") or {})
    if candidate.get("kind") == "virtual_audio_cable" and render_endpoint:
        return [render_endpoint]

    endpoints: list[dict[str, Any]] = []
    if candidate.get("kind") == "windows_stereo_mix_capture":
        endpoints.extend(portaudio_stereo_mix_render_candidates(dict(candidate.get("capture_endpoint") or {})))
    endpoints.extend(default_render_endpoint_candidates())
    endpoints.extend(
        dict(item) for item in list(route_status.get("render_endpoints") or [])
        if bool(item.get("active"))
    )
    if render_endpoint:
        endpoints.insert(0, render_endpoint)
    return dedupe_endpoints(endpoints)


def portaudio_stereo_mix_capture_candidates(capture_endpoint: dict[str, Any]) -> list[dict[str, Any]]:
    listing = list_portaudio_devices()
    if not listing.get("ok"):
        return []
    candidates = []
    vendor = endpoint_vendor_marker(capture_endpoint)
    for device in list(listing.get("devices") or []):
        name = str(device.get("name") or "").lower()
        if str(device.get("hostapi_name") or "") != "Windows WDM-KS":
            continue
        if int(device.get("max_input_channels") or 0) <= 0:
            continue
        if "立体声混音" not in name and "stereo" not in name:
            continue
        if vendor and vendor not in name:
            continue
        candidates.append(portaudio_endpoint_from_device(
            device,
            "capture",
            base_endpoint=capture_endpoint,
            synthetic="portaudio_wdmks_stereo_mix_capture",
        ))
    return candidates


def portaudio_stereo_mix_render_candidates(capture_endpoint: dict[str, Any]) -> list[dict[str, Any]]:
    listing = list_portaudio_devices()
    if not listing.get("ok"):
        return []
    candidates = []
    vendor = endpoint_vendor_marker(capture_endpoint)
    for device in list(listing.get("devices") or []):
        name = str(device.get("name") or "").lower()
        if str(device.get("hostapi_name") or "") != "Windows WDM-KS":
            continue
        if int(device.get("max_output_channels") or 0) <= 0:
            continue
        if vendor and vendor not in name:
            continue
        if vendor == "realtek" and not any(marker in name for marker in ("speaker", "speakers", "output", "扬声器")):
            continue
        candidates.append(portaudio_endpoint_from_device(
            device,
            "render",
            base_endpoint={},
            synthetic="portaudio_wdmks_stereo_mix_render",
        ))
    return candidates


def portaudio_endpoint_from_device(
    device: dict[str, Any],
    kind: str,
    *,
    base_endpoint: dict[str, Any] | None = None,
    synthetic: str = "",
) -> dict[str, Any]:
    base = dict(base_endpoint or {})
    endpoint = {
        "id": str(base.get("id") or ""),
        "full_id": str(base.get("full_id") or ""),
        "name": str(base.get("name") or device.get("name") or ""),
        "friendly_name": str(base.get("friendly_name") or ""),
        "provider": str(base.get("provider") or portaudio_provider_from_name(str(device.get("name") or ""))),
        "kind": kind,
        "state": str(base.get("state") or "active"),
        "active": bool(base.get("active", True)),
        "portaudio_device_index": int(device.get("index") or 0),
        "portaudio_hostapi_name": str(device.get("hostapi_name") or ""),
        "portaudio_name": str(device.get("name") or ""),
        "synthetic": synthetic,
    }
    return endpoint


def endpoint_vendor_marker(endpoint: dict[str, Any]) -> str:
    text = endpoint_search_text(endpoint)
    for marker in ("realtek", "todesk", "vb-audio", "voicemeeter", "hecate", "nvidia"):
        if marker in text:
            return marker
    return ""


def portaudio_provider_from_name(name: str) -> str:
    lowered = str(name or "").lower()
    if "realtek" in lowered:
        return "Realtek HD Audio"
    if "todesk" in lowered:
        return "ToDesk Virtual Audio"
    if "hecate" in lowered:
        return "HECATE"
    if "nvidia" in lowered:
        return "NVIDIA High Definition Audio"
    return ""


def default_render_endpoint_candidates() -> list[dict[str, Any]]:
    defaults = default_audio_endpoints("render")
    roles = defaults.get("roles") if isinstance(defaults.get("roles"), dict) else {}
    endpoints = []
    for role in DEFAULT_AUDIO_ROLES:
        item = roles.get(role) if isinstance(roles, dict) else {}
        endpoint = item.get("endpoint") if isinstance(item, dict) else {}
        if isinstance(endpoint, dict) and endpoint:
            endpoints.append(dict(endpoint))
    return dedupe_endpoints(endpoints)


def dedupe_endpoints(endpoints: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped = []
    seen: set[str] = set()
    for endpoint in endpoints:
        key = endpoint_identity(endpoint)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(dict(endpoint))
    return deduped


def endpoint_identity(endpoint: dict[str, Any]) -> str:
    portaudio_index = endpoint_portaudio_device_index(endpoint)
    if portaudio_index is not None:
        return f"portaudio:{portaudio_index}"
    return str(endpoint.get("full_id") or endpoint.get("id") or endpoint.get("name") or "").strip()


def compact_native_voice_route_status(route_status: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "ok",
        "status",
        "content_ready_candidate",
        "content_ready_proven",
        "content_loopback_state",
        "latest_loopback_diagnostic",
        "audio_file",
        "route_candidates",
        "default_capture_endpoints",
        "capture_endpoint_count",
        "render_endpoint_count",
        "active_capture_endpoint_count",
        "active_render_endpoint_count",
        "note",
        "next_action",
    )
    return {key: route_status.get(key) for key in keys if key in route_status}


def compact_loopback_diagnostic_result(
    result: dict[str, Any],
    *,
    attempt: dict[str, Any],
) -> dict[str, Any]:
    compact = {
        "ok": bool(result.get("ok")),
        "attempt": {
            "index": attempt.get("index"),
            "kind": attempt.get("kind"),
            "capture_endpoint": brief_endpoint(dict(attempt.get("capture_endpoint") or {})),
            "render_endpoint": brief_endpoint(dict(attempt.get("render_endpoint") or {})),
        },
    }
    for key in (
        "output_path",
        "source_path",
        "capture_device_index",
        "render_device_index",
        "source_sample_rate",
        "diagnostic_sample_rate",
        "source_stats",
        "capture_stats",
        "capture_band_energy",
        "correlation",
        "verdict",
        "capture_selection",
        "render_selection",
        "error",
    ):
        if key in result:
            compact[key] = result.get(key)
    return compact


def best_loopback_diagnostic_result(results: list[dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return {}

    def score(item: dict[str, Any]) -> tuple[float, float]:
        verdict = item.get("verdict") if isinstance(item.get("verdict"), dict) else {}
        correlation = float(item.get("correlation") or verdict.get("correlation") or 0.0)
        rms_ratio = float(verdict.get("rms_ratio") or 0.0)
        return (correlation, rms_ratio)

    return max(results, key=score)


def native_voice_loopback_verdict(
    source_stats: dict[str, Any],
    capture_stats: dict[str, Any],
    correlation: float,
    capture_band_energy: dict[str, Any],
) -> dict[str, Any]:
    source_rms = float(source_stats.get("rms") or 0.0)
    capture_rms = float(capture_stats.get("rms") or 0.0)
    rms_ratio = capture_rms / max(source_rms, 1e-9)
    voice_band = (
        float(capture_band_energy.get("80_300") or 0.0)
        + float(capture_band_energy.get("300_3400") or 0.0)
        + float(capture_band_energy.get("3400_8000") or 0.0)
    )
    low_band = float(capture_band_energy.get("0_80") or 0.0)
    low_frequency_bias = low_band > max(voice_band * 3.0, 1e-12)
    passed = (
        rms_ratio >= 0.02
        and float(correlation or 0.0) >= 0.15
        and not low_frequency_bias
    )
    if passed:
        recommendation = "Loopback signal looks usable for native WeChat voice bubble content."
    elif low_frequency_bias:
        recommendation = "Captured signal is dominated by sub-80Hz energy; this route is likely recording device noise, not TTS voice."
    elif rms_ratio < 0.02:
        recommendation = "Captured signal is too quiet; use a real virtual cable or a working stereo-mix route."
    else:
        recommendation = "Captured signal does not correlate with the source audio; verify the playback and capture endpoints are a true loopback pair."
    return {
        "passed": passed,
        "rms_ratio": rms_ratio,
        "correlation": float(correlation or 0.0),
        "low_frequency_bias": low_frequency_bias,
        "voice_band_energy": voice_band,
        "low_band_energy": low_band,
        "recommendation": recommendation,
    }


def _diagnostic_attempt_slug(attempt: dict[str, Any]) -> str:
    parts = [
        str(attempt.get("kind") or "route"),
        _endpoint_slug(dict(attempt.get("capture_endpoint") or {})),
        "to",
        _endpoint_slug(dict(attempt.get("render_endpoint") or {})),
    ]
    raw = "-".join(part for part in parts if part)
    normalized = "".join(char.lower() if char.isalnum() else "-" for char in raw)
    return "-".join(part for part in normalized.split("-") if part)[:96] or "route"


def _endpoint_slug(endpoint: dict[str, Any]) -> str:
    return str(endpoint.get("provider") or endpoint.get("friendly_name") or endpoint.get("name") or "endpoint")


def _run_loopback_diagnostic(
    audio_path: str | Path,
    capture_device_index: int,
    render_device_index: int,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    sd = _import_sounddevice()
    np = _import_numpy()
    rate, source = _read_wav_float32(audio_path)
    render_device = dict(sd.query_devices(int(render_device_index)))
    capture_device = dict(sd.query_devices(int(capture_device_index)))
    playback_rate = int(render_device.get("default_samplerate") or capture_device.get("default_samplerate") or rate)
    if playback_rate <= 0:
        playback_rate = rate
    playback = _prepare_playback_array(
        source,
        source_rate=rate,
        playback_rate=playback_rate,
        output_channels=max(1, min(2, int(render_device.get("max_output_channels") or 1))),
    )
    padding = np.zeros((int(playback_rate * 0.5), playback.shape[1]), dtype=np.float32)
    playback_padded = np.vstack([playback, padding]).astype(np.float32)
    recorded = sd.playrec(
        playback_padded,
        samplerate=playback_rate,
        channels=max(1, min(2, int(capture_device.get("max_input_channels") or 1))),
        dtype="float32",
        device=(int(capture_device_index), int(render_device_index)),
        blocking=True,
    )
    sd.stop()
    output = Path(output_path) if output_path else _default_loopback_output_path(audio_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_float32_wav(output, playback_rate, recorded)
    source_stats = _signal_stats(playback, playback_rate)
    capture_stats = _signal_stats(recorded, playback_rate)
    correlation = _normalized_correlation(playback, recorded)
    capture_band_energy = _band_energy(recorded, playback_rate)
    verdict = native_voice_loopback_verdict(
        source_stats,
        capture_stats,
        correlation,
        capture_band_energy,
    )
    return {
        "ok": True,
        "output_path": str(output),
        "source_path": str(audio_path),
        "capture_device_index": int(capture_device_index),
        "render_device_index": int(render_device_index),
        "source_sample_rate": int(rate),
        "diagnostic_sample_rate": int(playback_rate),
        "source_stats": source_stats,
        "capture_stats": capture_stats,
        "capture_band_energy": capture_band_energy,
        "correlation": correlation,
        "verdict": verdict,
    }


def _default_loopback_output_path(audio_path: str | Path) -> Path:
    source = Path(audio_path)
    base = source.parent if source.parent.exists() else Path.cwd()
    return base / f"{source.stem}-loopback-diagnostic.wav"


def _read_wav_float32(path: str | Path) -> tuple[int, Any]:
    np = _import_numpy()
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.getnframes()
        raw = handle.readframes(frames)
    if sample_width == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_width == 2:
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sample_width == 3:
        bytes_view = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        sign = (bytes_view[:, 2] >= 128).astype(np.int32) * -16777216
        values = (
            bytes_view[:, 0].astype(np.int32)
            | (bytes_view[:, 1].astype(np.int32) << 8)
            | (bytes_view[:, 2].astype(np.int32) << 16)
            | sign
        )
        data = values.astype(np.float32) / 8388608.0
    elif sample_width == 4:
        data = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"unsupported wav sample width: {sample_width}")
    if channels > 1:
        data = data.reshape(-1, channels)
    else:
        data = data.reshape(-1, 1)
    return int(rate), data.astype(np.float32, copy=False)


def _write_float32_wav(path: str | Path, rate: int, data: Any) -> None:
    np = _import_numpy()
    array = np.asarray(data, dtype=np.float32)
    if array.ndim == 1:
        array = array.reshape(-1, 1)
    clipped = np.clip(array, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(int(array.shape[1]))
        handle.setsampwidth(2)
        handle.setframerate(int(rate))
        handle.writeframes(pcm.tobytes())


def _prepare_playback_array(
    data: Any,
    *,
    source_rate: int,
    playback_rate: int,
    output_channels: int,
) -> Any:
    np = _import_numpy()
    prepared = np.asarray(data, dtype=np.float32)
    if int(source_rate) != int(playback_rate):
        prepared = _resample_array(prepared, int(source_rate), int(playback_rate))
    if prepared.ndim == 1:
        prepared = prepared.reshape(-1, 1)
    channels = max(1, int(output_channels or 1))
    if prepared.shape[1] == channels:
        return prepared
    if prepared.shape[1] == 1:
        return np.repeat(prepared, channels, axis=1)
    if prepared.shape[1] > channels:
        return prepared[:, :channels]
    repeats = channels - prepared.shape[1]
    tail = np.repeat(prepared[:, -1:], repeats, axis=1)
    return np.concatenate([prepared, tail], axis=1)


def _resample_array(data: Any, source_rate: int, target_rate: int) -> Any:
    np = _import_numpy()
    if source_rate <= 0 or target_rate <= 0 or source_rate == target_rate:
        return np.asarray(data, dtype=np.float32)
    try:
        from scipy import signal  # type: ignore

        rate_gcd = gcd(int(source_rate), int(target_rate))
        return signal.resample_poly(
            data,
            int(target_rate) // rate_gcd,
            int(source_rate) // rate_gcd,
            axis=0,
        ).astype(np.float32)
    except Exception:
        original = np.asarray(data, dtype=np.float32)
        if original.size == 0:
            return original
        target_frames = max(1, round(original.shape[0] * float(target_rate) / float(source_rate)))
        old_x = np.linspace(0.0, 1.0, num=original.shape[0], endpoint=False)
        new_x = np.linspace(0.0, 1.0, num=target_frames, endpoint=False)
        columns = [
            np.interp(new_x, old_x, original[:, channel]).astype(np.float32)
            for channel in range(original.shape[1])
        ]
        return np.stack(columns, axis=1)


def _signal_stats(data: Any, rate: int) -> dict[str, Any]:
    np = _import_numpy()
    mono = _mono(data)
    if mono.size <= 0:
        return {"frames": 0, "duration_seconds": 0.0, "rms": 0.0, "peak": 0.0}
    rms = float(np.sqrt(np.mean(np.square(mono))))
    peak = float(np.max(np.abs(mono)))
    return {
        "frames": int(mono.size),
        "duration_seconds": round(float(mono.size) / float(max(1, int(rate))), 3),
        "rms": rms,
        "peak": peak,
    }


def _band_energy(data: Any, rate: int) -> dict[str, float]:
    np = _import_numpy()
    mono = _mono(data)
    if mono.size < 32:
        return {"0_80": 0.0, "80_300": 0.0, "300_3400": 0.0, "3400_8000": 0.0}
    try:
        from scipy import signal  # type: ignore

        nperseg = min(2048, max(256, int(mono.size // 4)))
        freqs, psd = signal.welch(mono, fs=int(rate), nperseg=nperseg)
        def band(lo: float, hi: float) -> float:
            mask = (freqs >= lo) & (freqs < hi)
            return float(np.sum(psd[mask]))
    except Exception:
        freqs = np.fft.rfftfreq(int(mono.size), d=1.0 / float(max(1, int(rate))))
        spectrum = np.abs(np.fft.rfft(mono)) ** 2
        def band(lo: float, hi: float) -> float:
            mask = (freqs >= lo) & (freqs < hi)
            return float(np.sum(spectrum[mask]) / max(1, int(mono.size)))
    return {
        "0_80": band(0.0, 80.0),
        "80_300": band(80.0, 300.0),
        "300_3400": band(300.0, 3400.0),
        "3400_8000": band(3400.0, 8000.0),
    }


def _normalized_correlation(source: Any, capture: Any) -> float:
    np = _import_numpy()
    source_mono = _mono(source)
    capture_mono = _mono(capture)
    if source_mono.size < 16 or capture_mono.size < source_mono.size:
        return 0.0
    try:
        from scipy import signal  # type: ignore

        src = (source_mono - source_mono.mean()) / (source_mono.std() + 1e-9)
        cap = (capture_mono - capture_mono.mean()) / (capture_mono.std() + 1e-9)
        corr = signal.correlate(cap, src, mode="valid", method="fft") / max(1, int(src.size))
        if corr.size <= 0:
            return 0.0
        return float(np.max(np.abs(corr)))
    except Exception:
        src = source_mono[: min(source_mono.size, capture_mono.size)]
        cap = capture_mono[: src.size]
        src = (src - src.mean()) / (src.std() + 1e-9)
        cap = (cap - cap.mean()) / (cap.std() + 1e-9)
        return float(abs(np.mean(src * cap)))


def _mono(data: Any) -> Any:
    np = _import_numpy()
    array = np.asarray(data, dtype=np.float32)
    if array.ndim == 1:
        return array
    return array.mean(axis=1)


def _split_endpoint_tokens(text: str) -> list[str]:
    normalized = "".join(char.lower() if char.isalnum() else " " for char in str(text or ""))
    return [token for token in normalized.split() if token]


def _hostapi_name(hostapis: list[Any], index: int) -> str:
    try:
        return str((hostapis[index] or {}).get("name") or "")
    except Exception:
        return ""


def _import_sounddevice() -> Any:
    _ensure_runtime_python_deps_on_path()
    import sounddevice  # type: ignore

    return sounddevice


def _import_numpy() -> Any:
    import numpy  # type: ignore

    return numpy


def _ensure_runtime_python_deps_on_path() -> None:
    for deps in _runtime_python_deps_candidates():
        if deps.exists() and str(deps) not in sys.path:
            sys.path.insert(0, str(deps))


def _runtime_python_deps_candidates() -> list[Path]:
    candidates: list[Path] = []
    try:
        repo_root = Path(__file__).resolve().parents[2]
        candidates.append(repo_root / "runtime" / "python-deps")
    except Exception:
        pass
    candidates.append(Path.cwd() / "runtime" / "python-deps")
    unique: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        key = str(item)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _restore_previous_default_roles(
    policy: Any,
    previous: dict[str, Any],
    kind: str,
    restored_roles: list[dict[str, Any]],
    errors: list[str],
) -> None:
    previous_roles = dict(previous.get("roles") or {})
    for role_name, info in previous_roles.items():
        if role_name not in AUDIO_ROLES:
            continue
        if not isinstance(info, dict) or not info.get("ok"):
            continue
        previous_full_id = str(info.get("full_id") or mmdevice_full_id(str(info.get("id") or ""), kind))
        if not previous_full_id:
            continue
        try:
            policy.SetDefaultEndpoint(previous_full_id, audio_role_index(role_name))
            restored_roles.append({
                "role": role_name,
                "kind": kind,
                "ok": True,
                "restored_full_id": previous_full_id,
            })
        except Exception as exc:
            message = str(exc)
            errors.append(f"{kind}/{role_name}: {message}")
            restored_roles.append({
                "role": role_name,
                "kind": kind,
                "ok": False,
                "error": message,
                "restored_full_id": previous_full_id,
            })


def _verify_previous_default_roles(previous: dict[str, Any], after: dict[str, Any], kind: str) -> bool:
    verified = []
    previous_roles = dict(previous.get("roles") or {})
    for role_name, info in previous_roles.items():
        if role_name not in AUDIO_ROLES or not isinstance(info, dict) or not info.get("ok"):
            continue
        observed = ((after.get("roles") or {}).get(role_name) or {}).get("full_id", "")
        expected = str(info.get("full_id") or info.get("id") or "")
        verified.append(endpoint_identity_matches(str(observed), expected, kind=kind))
    return all(verified) if verified else True


def normalize_audio_roles(roles: Iterable[str] | None = None) -> tuple[str, ...]:
    if roles is None:
        return DEFAULT_AUDIO_ROLES
    normalized: list[str] = []
    for role in roles:
        role_name = str(role or "").strip().lower()
        if not role_name:
            continue
        if role_name not in AUDIO_ROLES:
            raise ValueError(f"unknown audio role: {role}")
        if role_name not in normalized:
            normalized.append(role_name)
    return tuple(normalized) or DEFAULT_AUDIO_ROLES


def audio_role_index(role: str | int) -> int:
    if isinstance(role, int):
        if role in AUDIO_ROLES.values():
            return int(role)
        raise ValueError(f"unknown audio role index: {role}")
    role_name = str(role or "").strip().lower()
    if role_name not in AUDIO_ROLES:
        raise ValueError(f"unknown audio role: {role}")
    return AUDIO_ROLES[role_name]


def audio_flow_index(kind: str | int) -> int:
    if isinstance(kind, int):
        if kind in AUDIO_FLOWS.values():
            return int(kind)
        raise ValueError(f"unknown audio flow index: {kind}")
    kind_name = str(kind or "").strip().lower()
    if kind_name not in AUDIO_FLOWS:
        raise ValueError(f"unknown audio flow: {kind}")
    return AUDIO_FLOWS[kind_name]


def core_audio_endpoint_state(endpoint_id: str, *, kind: str = "capture") -> dict[str, Any]:
    if os.name != "nt":
        return {"ok": False, "kind": kind, "error": "Windows Core Audio is required"}
    full_id = mmdevice_full_id(endpoint_id, kind)
    try:
        enumerator, _policy = _core_audio_objects()
        device = enumerator.GetDevice(full_id)
        actual_full_id = str(device.GetId() or full_id)
        state_code = int(device.GetState())
        endpoint = find_audio_endpoint(kind, actual_full_id)
        return {
            "ok": True,
            "kind": kind,
            "id": mmdevice_short_id(actual_full_id),
            "full_id": actual_full_id,
            "state_code": state_code,
            "state": device_state_name(state_code),
            "active": device_state_is_active(state_code),
            "endpoint": brief_endpoint(endpoint),
        }
    except Exception as exc:
        return {
            "ok": False,
            "kind": kind,
            "id": mmdevice_short_id(full_id),
            "full_id": full_id,
            "error": str(exc),
        }


def set_audio_endpoint_visibility(endpoint_id: str, *, visible: bool, kind: str = "capture") -> dict[str, Any]:
    if os.name != "nt":
        return {"ok": False, "kind": kind, "visible": visible, "error": "Windows Core Audio is required"}
    full_id = mmdevice_full_id(endpoint_id, kind)
    before = core_audio_endpoint_state(full_id, kind=kind)
    result: dict[str, Any] = {
        "ok": False,
        "kind": kind,
        "visible": bool(visible),
        "target_full_id": full_id,
        "before": before,
    }
    try:
        _enumerator, policy = _core_audio_objects()
        policy.SetEndpointVisibility(full_id, bool(visible))
    except Exception as exc:
        result["error"] = str(exc)
        return result
    after = core_audio_endpoint_state(full_id, kind=kind)
    result["after"] = after
    result["ok"] = bool(after.get("ok")) and bool(after.get("active")) == bool(visible)
    if not result["ok"]:
        result["error"] = "endpoint visibility did not reach requested state"
    return result


def _core_audio_objects() -> tuple[Any, Any]:
    _ensure_com_initialized()

    from ctypes import POINTER, c_int, c_longlong, c_void_p
    from ctypes.wintypes import BOOL, DWORD, LPWSTR

    import comtypes.client
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

    class IMMDevice(IUnknown):  # type: ignore
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"), (["in"], DWORD, "dwClsCtx"), (["in"], c_void_p, "pActivationParams"), (["out"], POINTER(c_void_p), "ppInterface")),
            COMMETHOD([], HRESULT, "OpenPropertyStore", (["in"], DWORD, "stgmAccess"), (["out"], POINTER(c_void_p), "ppProperties")),
            COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(LPWSTR), "ppstrId")),
            COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(DWORD), "pdwState")),
        ]

    class IMMDeviceEnumerator(IUnknown):  # type: ignore
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = [
            COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], c_int, "dataFlow"), (["in"], DWORD, "dwStateMask"), (["out"], POINTER(c_void_p), "ppDevices")),
            COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], c_int, "dataFlow"), (["in"], c_int, "role"), (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint")),
            COMMETHOD([], HRESULT, "GetDevice", (["in"], LPWSTR, "pwstrId"), (["out"], POINTER(POINTER(IMMDevice)), "ppDevice")),
            COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback", (["in"], c_void_p, "pClient")),
            COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback", (["in"], c_void_p, "pClient")),
        ]

    class IPolicyConfig(IUnknown):  # type: ignore
        _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetMixFormat", (["in"], LPWSTR, "wszDeviceId"), (["out"], POINTER(c_void_p), "ppFormat")),
            COMMETHOD([], HRESULT, "GetDeviceFormat", (["in"], LPWSTR, "wszDeviceId"), (["in"], BOOL, "bDefault"), (["out"], POINTER(c_void_p), "ppFormat")),
            COMMETHOD([], HRESULT, "ResetDeviceFormat", (["in"], LPWSTR, "wszDeviceId")),
            COMMETHOD([], HRESULT, "SetDeviceFormat", (["in"], LPWSTR, "wszDeviceId"), (["in"], c_void_p, "pEndpointFormat"), (["in"], c_void_p, "pMixFormat")),
            COMMETHOD([], HRESULT, "GetProcessingPeriod", (["in"], LPWSTR, "wszDeviceId"), (["in"], BOOL, "bDefault"), (["out"], POINTER(c_longlong), "pmftDefaultPeriod"), (["out"], POINTER(c_longlong), "pmftMinimumPeriod")),
            COMMETHOD([], HRESULT, "SetProcessingPeriod", (["in"], LPWSTR, "wszDeviceId"), (["in"], POINTER(c_longlong), "pmftPeriod")),
            COMMETHOD([], HRESULT, "GetShareMode", (["in"], LPWSTR, "wszDeviceId"), (["out"], c_void_p, "pMode")),
            COMMETHOD([], HRESULT, "SetShareMode", (["in"], LPWSTR, "wszDeviceId"), (["in"], c_void_p, "pMode")),
            COMMETHOD([], HRESULT, "GetPropertyValue", (["in"], LPWSTR, "wszDeviceId"), (["in"], c_void_p, "key"), (["out"], c_void_p, "pv")),
            COMMETHOD([], HRESULT, "SetPropertyValue", (["in"], LPWSTR, "wszDeviceId"), (["in"], c_void_p, "key"), (["in"], c_void_p, "pv")),
            COMMETHOD([], HRESULT, "SetDefaultEndpoint", (["in"], LPWSTR, "wszDeviceId"), (["in"], c_int, "role")),
            COMMETHOD([], HRESULT, "SetEndpointVisibility", (["in"], LPWSTR, "wszDeviceId"), (["in"], BOOL, "bVisible")),
        ]

    enumerator = comtypes.client.CreateObject(
        GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
        interface=IMMDeviceEnumerator,
    )
    policy = comtypes.client.CreateObject(
        GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}"),
        interface=IPolicyConfig,
    )
    return enumerator, policy


def _ensure_com_initialized() -> None:
    if os.name != "nt" or getattr(_COM_THREAD_STATE, "initialized", False):
        return
    import comtypes

    try:
        comtypes.CoInitialize()
    except Exception as exc:
        hresult = getattr(exc, "hresult", None)
        winerror = getattr(exc, "winerror", None)
        if hresult in (-2147417850, 0x80010106) or winerror in (-2147417850, 0x80010106):
            _COM_THREAD_STATE.initialized = True
            return
        raise
    _COM_THREAD_STATE.initialized = True
