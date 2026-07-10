"""统一素材目录：在运行根目录建一个 assets/ 父文件夹(persona/voices/stickers/images)，
把旧的 .data/media-assets/* 自动迁移过来并把 config 指过去。便于用户集中放素材。

机器数据(状态/队列/收到的媒体/合成语音/manifest)仍留在 runtime/.data，不动。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .config import AppConfig, resolve_relative, write_config_atomic

# 子目录 → 放进各个 README 的一句话说明
ASSET_SUBDIRS: dict[str, str] = {
    "persona": "把人格文件(如 SKILL.md / memory.md)放这里，并在「人格模式」里把人格目录指向本文件夹。",
    "voices": "把语音克隆用的参考音频(.wav)放这里。",
    "stickers": "把动画表情 / GIF(.gif)放这里。",
    "images": "把要发送的图片放这里。",
}


def run_root(config: AppConfig) -> Path:
    parent = Path(config.path).resolve().parent
    return parent.parent if parent.name.lower() == "runtime" else parent


def _move_dir_files(src: Path, dst: Path) -> int:
    if not src.exists() or src.resolve() == dst.resolve():
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    moved = 0
    for item in src.iterdir():
        if item.is_file():
            target = dst / item.name
            if not target.exists():
                try:
                    shutil.move(str(item), str(target))
                    moved += 1
                except OSError:
                    pass
    return moved


def ensure_assets_layout(config: AppConfig) -> dict[str, Any]:
    """建好 assets/ 各子目录(带说明)，迁移旧 images/gifs，并把 config 指向新目录(变更才落盘)。"""
    assets = run_root(config) / "assets"
    for name, tip in ASSET_SUBDIRS.items():
        d = assets / name
        d.mkdir(parents=True, exist_ok=True)
        readme = d / "说明.txt"
        if not readme.exists():
            try:
                readme.write_text(tip + "\n", encoding="utf-8")
            except OSError:
                pass

    images_target = assets / "images"
    stickers_target = assets / "stickers"
    moved = {"images": 0, "stickers": 0}
    changed = False

    cur_img = resolve_relative(config.path, config.media.image_assets_dir) if config.media.image_assets_dir else None
    if cur_img is None or cur_img.resolve() != images_target.resolve():
        if cur_img is not None:
            moved["images"] = _move_dir_files(cur_img, images_target)
        config.media.image_assets_dir = str(images_target)
        changed = True

    cur_gif = resolve_relative(config.path, config.media.gif_assets_dir) if config.media.gif_assets_dir else None
    if cur_gif is None or cur_gif.resolve() != stickers_target.resolve():
        if cur_gif is not None:
            moved["stickers"] = _move_dir_files(cur_gif, stickers_target)
        config.media.gif_assets_dir = str(stickers_target)
        changed = True

    if changed and config.path.exists():
        raw = json.loads(config.path.read_text(encoding="utf-8"))
        media_raw = dict(raw.get("media") or {})
        media_raw["image_assets_dir"] = config.media.image_assets_dir
        media_raw["gif_assets_dir"] = config.media.gif_assets_dir
        raw["media"] = media_raw
        write_config_atomic(config, raw)

    return {
        "assets_root": str(assets),
        "persona": str(assets / "persona"),
        "voices": str(assets / "voices"),
        "stickers": str(stickers_target),
        "images": str(images_target),
        "migrated": moved,
        "repointed": changed,
    }
