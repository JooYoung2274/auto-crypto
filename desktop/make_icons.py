"""앱 아이콘 생성 — 캔들스틱 마크.

    python desktop/make_icons.py

산출물 (모두 커밋되므로 CI에서 재생성하지 않는다):
    desktop/assets/icon.png      1024px 원본 (스토어·판매 페이지용)
    desktop/assets/icon.ico      Windows 실행 파일용 (16~256 멀티 사이즈)
    desktop/assets/icon.icns     macOS 번들용 (macOS에서 실행할 때만 생성)
    frontend/public/favicon.png  웹 UI 탭 아이콘

작은 크기 legibility가 설계 제약이다. 16px에서 디테일은 전부 뭉개지므로
얇은 선·그라데이션·글자를 쓰지 않고, 굵은 캔들 3개만 남겼다.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "desktop" / "assets"

SIZE = 1024
INSET = 96                     # macOS 스퀴클 그리드에 맞춘 여백
RADIUS = 190

BG_TOP = (26, 22, 43)          # #1A162B
BG_BOTTOM = (18, 15, 30)       # #120F1E
UP = (52, 211, 153)            # #34D399
DOWN = (228, 87, 76)           # #E4574C

# (색, 심지 시작, 심지 끝, 몸통 시작, 몸통 끝) — 차트 높이 대비 비율(0=위)
CANDLES = [
    (UP, 0.44, 0.92, 0.55, 0.85),
    (DOWN, 0.14, 0.70, 0.24, 0.60),
    (UP, 0.08, 0.56, 0.18, 0.45),
]


def _background() -> Image.Image:
    """세로 그라데이션을 깐 라운드 스퀘어. 투명 여백을 남겨 macOS 그리드에 맞춘다."""
    grad = Image.new("RGB", (1, SIZE))
    for y in range(SIZE):
        t = y / (SIZE - 1)
        grad.putpixel(
            (0, y),
            tuple(round(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)),
        )
    grad = grad.resize((SIZE, SIZE))

    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (INSET, INSET, SIZE - INSET, SIZE - INSET), radius=RADIUS, fill=255
    )
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    return img


def _draw_candles(img: Image.Image) -> None:
    d = ImageDraw.Draw(img)
    pad = 78
    left, right = INSET + pad, SIZE - INSET - pad
    top, bottom = INSET + pad, SIZE - INSET - pad
    height = bottom - top

    # 16px에서도 캔들 3개가 구분되도록 몸통을 굵게 잡는다.
    body_w, wick_w = 192, 46
    gap = ((right - left) - body_w * len(CANDLES)) / (len(CANDLES) - 1)

    for i, (color, wick_a, wick_b, body_a, body_b) in enumerate(CANDLES):
        cx = left + i * (body_w + gap) + body_w / 2
        d.rounded_rectangle(
            (cx - wick_w / 2, top + height * wick_a, cx + wick_w / 2, top + height * wick_b),
            radius=wick_w / 2,
            fill=color,
        )
        d.rounded_rectangle(
            (cx - body_w / 2, top + height * body_a, cx + body_w / 2, top + height * body_b),
            radius=26,
            fill=color,
        )


def build_master() -> Image.Image:
    img = _background()
    _draw_candles(img)
    return img


def write_ico(master: Image.Image, path: Path) -> None:
    # Pillow가 멀티 사이즈 .ico를 직접 쓴다. 16px까지 넣어야 탐색기 목록 보기에서
    # 흐릿하게 늘어나지 않는다.
    sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256)]
    master.save(path, format="ICO", sizes=sizes)


def write_icns(master: Image.Image, path: Path) -> bool:
    """macOS `iconutil`로 .icns 생성. 다른 OS에서는 건너뛴다."""
    if sys.platform != "darwin" or not shutil.which("iconutil"):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        for size in (16, 32, 64, 128, 256, 512):
            master.resize((size, size), Image.LANCZOS).save(
                iconset / f"icon_{size}x{size}.png"
            )
            master.resize((size * 2, size * 2), Image.LANCZOS).save(
                iconset / f"icon_{size}x{size}@2x.png"
            )
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(path)], check=True
        )
    return True


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    master = build_master()

    master.save(ASSETS / "icon.png")
    write_ico(master, ASSETS / "icon.ico")
    made_icns = write_icns(master, ASSETS / "icon.icns")

    favicon = ROOT / "frontend" / "public" / "favicon.png"
    master.resize((64, 64), Image.LANCZOS).save(favicon)

    print(f"생성: {ASSETS / 'icon.png'}")
    print(f"생성: {ASSETS / 'icon.ico'}")
    print(f"생성: {ASSETS / 'icon.icns'}" if made_icns else "건너뜀: icon.icns (macOS 필요)")
    print(f"생성: {favicon}")


if __name__ == "__main__":
    main()
