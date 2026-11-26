"""Generate GPUDock's UI imagery with OpenAI's image API.

All images share one art direction (studio product photography on warm paper,
unbranded hardware, no text) so the UI feels designed rather than templated.

Key lookup: $OPENAI_API_KEY, else ~/.config/gpudock/openai.key (one line).
The key is never printed. Output: apps/web/public/img/<name>.webp

    uv run --project packages/core python scripts/gen_images.py            # missing images only
    uv run --project packages/core python scripts/gen_images.py --force hero hw-l4
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import time
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parents[1] / "apps/web/public/img"
MODELS = [os.environ.get("OPENAI_IMAGE_MODEL") or "gpt-image-2", "gpt-image-1.5", "gpt-image-1"]

STYLE = (
    "Studio product photograph on a seamless warm off-white paper backdrop (hex #F1EEE7). "
    "Soft diffused daylight from the upper left, a gentle long soft shadow. Realistic matte materials, "
    "fine detail, shallow depth of field kept subtle. Generic, unbranded hardware: absolutely no logos, "
    "no brand names, no text, no numbers, no labels or stickers. Calm, precise, editorial — like a design "
    "museum catalogue. Muted palette; the only saturated colour allowed is a tiny signal-orange accent if it fits."
)

IMAGES: dict[str, tuple[str, str]] = {
    "hero": (
        "1536x1024",
        "A single unbranded data-center GPU accelerator card lying at a slight angle on the paper, its metal "
        "heatsink fins catching the light, a thin black power cable curling away to the right. The object sits "
        "in the right two thirds of the frame; generous empty paper on the left for text.",
    ),
    "hw-t4": ("1024x1024", "A compact low-profile single-slot passive accelerator card with a matte black shroud and a short green-black circuit board with a gold edge connector, three-quarter view, centred."),
    "hw-l4": ("1024x1024", "A compact low-profile single-slot passive accelerator card with a dark graphite shroud with a fine ribbed texture and one tiny orange status LED, three-quarter view, centred."),
    "hw-a100": ("1024x1024", "A full-height dual-slot passive data-center accelerator card with a champagne-gold brushed-metal shroud and cut-out fins, three-quarter view, centred."),
    "hw-g4": ("1024x1024", "A full-size dual-slot workstation graphics card with a black blower-style shroud and a single radial fan at one end, three-quarter view, centred."),
    "hw-h100": ("1024x1024", "A flat mezzanine-style accelerator module seen from above at a slight angle: a square black heatsink block on a dark circuit board edged with dense gold contacts, centred."),
    "hw-tpu": ("1024x1024", "A square server accelerator board with one large chip under a low aluminium heatsink, viewed from above at an angle, centred."),
    "hw-cpu": ("1024x1024", "A single unbranded server CPU package with a silver metal heat spreader and gold pads underneath, macro close-up, centred."),
    "feat-dub": ("1024x1024", "A vintage studio microphone on a small stand next to a pair of closed-back headphones, centred."),
    "feat-transcribe": ("1024x1024", "A small neat stack of printed paper pages next to a pencil and a small reel of magnetic tape, top-down three-quarter view, centred."),
    "feat-tts": ("1024x1024", "A small minimalist bookshelf speaker in warm grey fabric next to an open notebook, centred."),
    "feat-models": ("1024x1024", "Four small unlabeled grey hardware cartridges standing upright in a neat row, centred."),
    "feat-video": ("1024x1024", "A compact vintage film movie camera on a small tripod, matte black and brushed metal, centred."),
    "feat-image": ("1024x1024", "A few loose sheets of thick watercolour paper with soft abstract colour washes, a single brush resting across them, centred."),
}


def api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    path = Path.home() / ".config/gpudock/openai.key"
    if not key and path.exists():
        key = path.read_text().strip()
    if not key:
        sys.exit("没有找到 OpenAI Key：设置 OPENAI_API_KEY，或把 Key 写入 ~/.config/gpudock/openai.key")
    return key


def generate(client: httpx.Client, name: str, size: str, subject: str, quality: str) -> bytes:
    prompt = f"{subject} {STYLE}"
    last = None
    for model in dict.fromkeys(MODELS):
        body = {"model": model, "prompt": prompt, "size": size, "quality": quality, "n": 1, "output_format": "webp", "output_compression": 82}
        r = client.post("https://api.openai.com/v1/images/generations", json=body)
        if r.status_code == 200:
            print(f"  {name}: {model}")
            return base64.b64decode(r.json()["data"][0]["b64_json"])
        last = f"{model}: HTTP {r.status_code} {r.text[:200]}"
        if r.status_code in (400, 404) and "model" in r.text.lower():
            continue  # try an older model name
        if r.status_code == 429:
            time.sleep(20)
            continue
        break
    raise RuntimeError(last)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("names", nargs="*", help="only these images (default: all)")
    p.add_argument("--force", action="store_true", help="regenerate even if the file exists")
    p.add_argument("--quality", default="medium", choices=["low", "medium", "high"])
    args = p.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    key = api_key()
    names = args.names or list(IMAGES)
    with httpx.Client(timeout=300, headers={"Authorization": f"Bearer {key}"}) as client:
        for name in names:
            target = OUT / f"{name}.webp"
            if target.exists() and not args.force:
                print(f"  {name}: 已存在，跳过")
                continue
            size, subject = IMAGES[name]
            target.write_bytes(generate(client, name, size, subject, args.quality))
            print(f"    -> {target.relative_to(OUT.parents[2])} ({target.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
