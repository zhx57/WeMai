from __future__ import annotations

from typing import Any


def runtime_connector_defaults(connector_id: str) -> dict[str, Any]:
    connector_id = str(connector_id or "").strip() or "runtime_connector"
    normalized = connector_id.lower()

    if normalized == "onebot_test" or normalized.startswith("onebot_test"):
        return {
            "type": "onebot_test",
            "platform": "onebot_v11_test",
            "driver": "adapter_queue_smoke",
            "enabled": True,
            "status": "runtime-test",
            "capabilities": {
                "queue_poll": "ready",
                "queue_ack": "ready",
                "text_in": "ready-simulated",
                "text_out": "ready-queue-contract",
                "image_in": "ready-simulated",
                "image_out": "ready-queue-contract",
                "voice_out": "ready-queue-contract-not-real-wechat",
                "animated_sticker_out": "ready-queue-contract-not-real-wechat",
                "realtime_voice": "unsupported",
                "media_execution": "not-real-wechat",
            },
            "notes": "OneBot/Akasha/AstrBot safety connector for queue smoke tests; it proves adapter contracts, not real WeChat delivery.",
        }

    if normalized == "weflow_http" or normalized.startswith("weflow_http"):
        return {
            "type": "weflow_http",
            "platform": "windows_wechat_weflow",
            "driver": "weflow_http",
            "enabled": True,
            "status": "reserved-legacy-http-route",
            "capabilities": {
                "queue_poll": "ready",
                "queue_ack": "ready",
                "text_out": "reserved-legacy-route-missing-current-weflow",
                "image_out": "unsupported-by-current-weflow-http",
                "voice_out": "unsupported-by-weflow-http",
                "animated_sticker_out": "unsupported-by-weflow-http",
                "realtime_voice": "unsupported",
            },
            "notes": "Reserved WeFlow HTTP sender for legacy/third-party gateways; current installed WeFlow HTTP has no send route.",
        }

    if normalized == "weflow" or normalized.startswith("weflow_"):
        return {
            "type": "weflow",
            "platform": "windows_wechat_weflow",
            "driver": "weflow_sse",
            "enabled": True,
            "status": "inbound-ready",
            "capabilities": {
                "text_in": "ready-sse",
                "media_in": "ready-sse-media",
                "image_in": "ready-sse-media",
                "voice_in": "ready-sse-media",
                "animated_sticker_in": "ready-sse-media",
                "text_out": "unsupported-by-weflow",
                "image_out": "unsupported-by-weflow",
                "voice_out": "unsupported-by-weflow",
                "animated_sticker_out": "unsupported-by-weflow",
                "realtime_voice": "unsupported",
            },
            "notes": "WeFlow SSE inbound runtime connector; text, images, voice, and animated stickers are received as one inbound message object with availability status and source refs.",
        }

    if _looks_like_mobile_runtime(normalized, "ios"):
        return {
            "type": "ios_phone",
            "platform": "ios_wechat",
            "driver": "ios_wda_reserved",
            "enabled": True,
            "status": "runtime-reserved",
            "capabilities": _mobile_runtime_capabilities("ios"),
            "notes": "Runtime iOS connector reservation; poll/ack contract is ready, live media execution still needs WDA/device implementation.",
        }

    if _looks_like_mobile_runtime(normalized, "android"):
        return {
            "type": "android_phone",
            "platform": "android_wechat",
            "driver": "android_appium_reserved",
            "enabled": True,
            "status": "runtime-reserved",
            "capabilities": _mobile_runtime_capabilities("android"),
            "notes": "Runtime Android connector reservation; poll/ack contract is ready, live media execution still needs Appium/device implementation.",
        }

    return {
        "type": "runtime_connector",
        "platform": "wechat_client",
        "driver": "poll_bridge",
        "enabled": True,
        "status": "runtime",
        "capabilities": {
            "queue_poll": "ready",
            "queue_ack": "ready",
            "text_in": "connector-dependent",
            "text_out": "connector-dependent",
            "image_in": "connector-dependent",
            "image_out": "connector-dependent",
            "voice_out": "connector-dependent",
            "animated_sticker_out": "connector-dependent",
            "realtime_voice": "connector-dependent",
        },
        "notes": "Runtime connector discovered from queue activity; media support depends on the bridge that polls and acks this id.",
    }


def connector_capabilities(connector: dict[str, Any]) -> dict[str, str]:
    raw = connector.get("capabilities")
    if isinstance(raw, dict) and raw:
        return {str(key): str(value) for key, value in raw.items()}

    connector_id = str(connector.get("id") or connector.get("name") or "").strip()
    if connector_id:
        inferred = runtime_connector_defaults(connector_id).get("capabilities")
        if isinstance(inferred, dict):
            return {str(key): str(value) for key, value in inferred.items()}

    connector_type = str(connector.get("type") or "").strip().lower()
    driver = str(connector.get("driver") or "").strip().lower()
    platform = str(connector.get("platform") or "").strip().lower()

    if driver == "cloud_queue" or connector_type == "cloud_queue":
        return {
            "queue_poll": "ready",
            "queue_ack": "ready",
            "text_action": "ready",
            "voice_action": "ready-for-capable-connector",
            "animated_sticker_action": "ready-for-capable-connector",
        }

    if connector_type == "onebot_test":
        return runtime_connector_defaults("onebot_test")["capabilities"]

    if connector_type == "ios_phone" or "ios" in platform:
        return _mobile_runtime_capabilities("ios")

    if connector_type == "android_phone" or "android" in platform:
        return _mobile_runtime_capabilities("android")

    if driver == "poll_bridge":
        return runtime_connector_defaults(connector_id or "runtime_connector")["capabilities"]

    return {
        "text_in": "unknown",
        "text_out": "unknown",
    }


def _media_in_summary_parts(capabilities: dict[str, str]) -> list[str]:
    subtype_labels = {
        "image_in": "图片",
        "voice_in": "语音",
        "animated_sticker_in": "动画表情",
    }
    present_subtypes = [(key, label) for key, label in subtype_labels.items() if key in capabilities]
    if "media_in" not in capabilities and not present_subtypes:
        return []

    if "media_in" in capabilities:
        media_state = capabilities["media_in"]
    else:
        subtype_states = {capabilities[key] for key, _ in present_subtypes}
        media_state = next(iter(subtype_states)) if len(subtype_states) == 1 else "mixed"

    subtype_parts = []
    for key, label in present_subtypes:
        subtype_state = capabilities[key]
        subtype_parts.append(label if subtype_state == media_state else f"{label}:{subtype_state}")
    suffix = f"（包含{'/'.join(subtype_parts)}）" if subtype_parts else ""
    return [f"收媒体:{media_state}{suffix}"]


def capability_summary(capabilities: dict[str, str]) -> str:
    preferred = [
        ("text_in", "收文字"),
        ("text_out", "发文字"),
        ("image_out", "发图片"),
        ("voice_out", "发语音"),
        ("animated_sticker_out", "发动画表情"),
        ("realtime_voice", "实时语音"),
        ("queue_poll", "轮询"),
        ("queue_ack", "确认"),
    ]
    parts = []
    if "text_in" in capabilities:
        parts.append(f"收文字:{capabilities['text_in']}")
    media_parts = _media_in_summary_parts(capabilities)
    if media_parts:
        parts.extend(media_parts)
    for key, label in preferred:
        if key == "text_in":
            continue
        if key in capabilities:
            parts.append(f"{label}:{capabilities[key]}")
    if parts:
        return " / ".join(parts)
    return " / ".join(f"{key}:{value}" for key, value in capabilities.items())


def _looks_like_mobile_runtime(normalized_id: str, platform: str) -> bool:
    return normalized_id == f"{platform}_phone" or normalized_id.startswith(f"{platform}_phone_")


def _mobile_runtime_capabilities(platform: str) -> dict[str, str]:
    label = "ios" if platform == "ios" else "android"
    return {
        "queue_poll": "ready",
        "queue_ack": "ready",
        "text_in": f"planned-{label}-connector",
        "text_out": f"planned-{label}-connector",
        "image_in": f"planned-{label}-connector",
        "image_out": f"planned-{label}-connector",
        "voice_in": f"planned-{label}-connector",
        "voice_out": "planned-native-bubble-not-live-proven",
        "animated_sticker_out": "planned-ui-not-live-proven",
        "realtime_voice": "planned-call-ui-not-live-proven",
    }
