"""Same-origin frontend static asset hosting."""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def mount_frontend(app: FastAPI, dist_dir: Path) -> None:
    """Mount a built Vite SPA; unknown APIs stay 404 and deep links use index."""
    index_file = dist_dir / "index.html"
    if not index_file.is_file():
        return

    dist_root = dist_dir.resolve()
    assets_dir = dist_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404)
        candidate = (dist_dir / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(dist_root):
            if candidate.suffix.lower() in {".csv", ".tsv", ".xls", ".xlsx"}:
                return FileResponse(candidate, filename=candidate.name)
            return FileResponse(candidate)
        return FileResponse(index_file)
