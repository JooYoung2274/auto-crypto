# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 스펙 — Coin Agents Office 모의거래 데스크탑 앱.

빌드: cd desktop && pyinstaller coinagent.spec
산출물: desktop/dist/CoinAgentsOffice(.app / .exe)

프론트엔드는 먼저 빌드돼 있어야 한다: cd frontend && npm run build
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve().parent  # desktop/ 의 부모 = 저장소 루트
BACKEND = ROOT / "backend"
FRONTEND_DIST = ROOT / "frontend" / "dist"

if not FRONTEND_DIST.is_dir():
    raise SystemExit("frontend/dist 없음 — 먼저 `cd frontend && npm run build`")

# 무거운/동적 임포트 패키지는 통째로 수집.
datas, binaries, hiddenimports = [], [], []
for pkg in ("uvicorn", "fastapi", "starlette", "pydantic", "pydantic_settings",
            "httpx", "httpcore", "pandas", "numpy", "webview", "anyio"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

hiddenimports += collect_submodules("app")
# 빌드된 프론트엔드를 번들에 포함 (main.py가 _MEIPASS/frontend_dist에서 찾음).
datas += [(str(FRONTEND_DIST), "frontend_dist")]
# 기본 챔피언 시드 JSON — 패키지 안의 데이터 파일이라 자동 수집되지 않는다.
# 빠지면 첫 실행에 챔피언이 없어 30~45분 연구 사이클을 돌려야 한다.
SEED_JSON = BACKEND / "app" / "seed" / "default_champion.json"
if not SEED_JSON.is_file():
    raise SystemExit(f"기본 챔피언 시드 없음: {SEED_JSON}")
datas += [(str(SEED_JSON), "app/seed")]

# 앱 아이콘 — 커밋된 산출물 (재생성: python desktop/make_icons.py).
ASSETS = ROOT / "desktop" / "assets"
ICON_ICNS = ASSETS / "icon.icns"
ICON_ICO = ASSETS / "icon.ico"
if sys.platform == "darwin":
    APP_ICON = ICON_ICNS
elif sys.platform.startswith("win"):
    APP_ICON = ICON_ICO
else:
    APP_ICON = None             # Linux는 실행 파일에 아이콘을 박지 않는다
if APP_ICON is not None and not APP_ICON.is_file():
    raise SystemExit(f"앱 아이콘 없음: {APP_ICON} — `python desktop/make_icons.py` 실행")

block_cipher = None

a = Analysis(
    [str(Path(SPECPATH) / "launcher.py")],
    pathex=[str(BACKEND)],          # backend/app 임포트 경로
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "pytest"],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="CoinAgentsOffice",
    debug=False,
    strip=False,
    upx=False,
    console=False,                  # GUI 앱 (콘솔 창 없음)
    icon=str(APP_ICON) if APP_ICON else None,
)
coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, name="CoinAgentsOffice",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="CoinAgentsOffice.app",
        icon=str(ICON_ICNS),
        bundle_identifier="com.coinagent.office",
        info_plist={
            "NSHighResolutionCapable": True,
            "LSApplicationCategoryType": "public.app-category.finance",
        },
    )
