from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse

PWA_DIR = Path(__file__).resolve().parent

# PWA 页面与静态文件的统一路由前缀
PWA_BASE = "/otty"


NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


def mount_pwa(app: FastAPI) -> None:
    """Mount the standalone PWA workspace client routes onto a FastAPI app."""

    @app.get("/", include_in_schema=False)
    async def pwa_root_redirect():
        return RedirectResponse(PWA_BASE + "/", status_code=307)

    @app.get(PWA_BASE, include_in_schema=False)
    async def pwa_index():
        return FileResponse(
            PWA_DIR / "index.html",
            media_type="text/html; charset=utf-8",
            headers=NO_CACHE_HEADERS,
        )

    @app.get(PWA_BASE + "/manifest.json", include_in_schema=False)
    async def pwa_manifest():
        return FileResponse(
            PWA_DIR / "manifest.json",
            media_type="application/manifest+json",
            headers=NO_CACHE_HEADERS,
        )

    @app.get(PWA_BASE + "/sw.js", include_in_schema=False)
    async def pwa_sw():
        return FileResponse(
            PWA_DIR / "sw.js",
            media_type="application/javascript; charset=utf-8",
            headers=NO_CACHE_HEADERS,
        )

    @app.get(PWA_BASE + "/icon.svg", include_in_schema=False)
    async def pwa_icon():
        return FileResponse(
            PWA_DIR / "icon.svg",
            media_type="image/svg+xml",
        )

    @app.get(PWA_BASE + "/styles.css", include_in_schema=False)
    async def pwa_styles():
        return FileResponse(
            PWA_DIR / "styles.css",
            media_type="text/css; charset=utf-8",
            headers=NO_CACHE_HEADERS,
        )

    @app.get(PWA_BASE + "/app.js", include_in_schema=False)
    async def pwa_script():
        return FileResponse(
            PWA_DIR / "app.js",
            media_type="application/javascript; charset=utf-8",
            headers=NO_CACHE_HEADERS,
        )
