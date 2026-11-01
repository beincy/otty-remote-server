from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

PWA_DIR = Path(__file__).resolve().parent

NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _mount_pwa_at(app: FastAPI, base: str) -> None:
    @app.get(base + "/", include_in_schema=False)
    async def pwa_index():
        return FileResponse(
            PWA_DIR / "index.html",
            media_type="text/html; charset=utf-8",
            headers=NO_CACHE_HEADERS,
        )

    @app.get(base + "/manifest.json", include_in_schema=False)
    async def pwa_manifest():
        return FileResponse(
            PWA_DIR / "manifest.json",
            media_type="application/manifest+json",
            headers=NO_CACHE_HEADERS,
        )

    @app.get(base + "/sw.js", include_in_schema=False)
    async def pwa_sw():
        return FileResponse(
            PWA_DIR / "sw.js",
            media_type="application/javascript; charset=utf-8",
            headers=NO_CACHE_HEADERS,
        )

    @app.get(base + "/icon.svg", include_in_schema=False)
    async def pwa_icon():
        return FileResponse(
            PWA_DIR / "icon.svg",
            media_type="image/svg+xml",
        )

    @app.get(base + "/styles.css", include_in_schema=False)
    async def pwa_styles():
        return FileResponse(
            PWA_DIR / "styles.css",
            media_type="text/css; charset=utf-8",
            headers=NO_CACHE_HEADERS,
        )

    @app.get(base + "/app.js", include_in_schema=False)
    async def pwa_script():
        return FileResponse(
            PWA_DIR / "app.js",
            media_type="application/javascript; charset=utf-8",
            headers=NO_CACHE_HEADERS,
        )


def mount_pwa(app: FastAPI) -> None:
    """Mount the standalone PWA workspace client routes onto a FastAPI app.

    Served at both "/" and "/otty" so it works:
    - directly: http://host:8020/ or http://host:8020/otty/
    - behind a prefix-stripping reverse proxy (e.g. Express
      ``app.use('/otty', proxy(...))`` + cpolar): the browser still requests
      /otty/*, which reaches uvicorn as /*, so the root copies must exist.
    """
    _mount_pwa_at(app, "")
    _mount_pwa_at(app, "/otty")
