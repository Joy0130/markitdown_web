"""MarkItDown Web — 將文件轉成 Markdown 的網頁服務。

Endpoints
    GET  /                → 前端頁面
    GET  /healthz         → 健康檢查
    GET  /api/formats     → 支援的副檔名清單
    POST /api/convert     → multipart 批次上傳，回傳每個檔案的 Markdown
    POST /api/convert-url → 由 URL 抓取並轉換
    POST /api/zip         → 把前端已取得的結果打包成 zip
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import tempfile
import unicodedata
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from markitdown import MarkItDown

# --------------------------------------------------------------------------- #
# 設定
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "50"))
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
MAX_FILES = int(os.getenv("MAX_FILES", "20"))
WORKERS = int(os.getenv("CONVERT_WORKERS", str(min(8, (os.cpu_count() or 2) * 2))))
ENABLE_PLUGINS = os.getenv("MARKITDOWN_ENABLE_PLUGINS", "0") == "1"
ENABLE_URL_FETCH = os.getenv("ENABLE_URL_FETCH", "1") == "1"

SUPPORTED_EXTENSIONS = sorted(
    {
        ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls", ".csv", ".tsv",
        ".html", ".htm", ".xml", ".json", ".txt", ".md", ".markdown", ".rst",
        ".epub", ".msg", ".zip", ".ipynb",
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff",
        ".mp3", ".wav", ".m4a", ".flac",
    }
)

_executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="markitdown")

app = FastAPI(title="MarkItDown Web", version="1.0.0", docs_url="/api/docs", redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=1024)


# --------------------------------------------------------------------------- #
# 工具函式
# --------------------------------------------------------------------------- #

def _new_converter() -> MarkItDown:
    """每次轉換建立獨立實例，避免多執行緒共用狀態。"""
    return MarkItDown(enable_plugins=ENABLE_PLUGINS)


def safe_stem(name: str) -> str:
    """把上傳檔名正規化成安全的輸出檔名主體。"""
    name = unicodedata.normalize("NFKC", Path(name or "untitled").name)
    stem = Path(name).stem or "untitled"
    stem = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", stem).strip(" .")
    return (stem or "untitled")[:120]


def _convert_bytes(data: bytes, filename: str) -> str:
    """MarkItDown 依副檔名挑選 converter，因此寫入保留副檔名的暫存檔。"""
    suffix = Path(filename).suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        return _new_converter().convert(tmp_path).text_content or ""
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _convert_uri(uri: str) -> tuple[str, str]:
    md = _new_converter()
    result = md.convert_uri(uri) if hasattr(md, "convert_uri") else md.convert(uri)
    title = (getattr(result, "title", None) or Path(uri.split("?")[0]).stem or "url")
    return safe_stem(title), (result.text_content or "")


async def _run(fn, *args) -> Any:
    return await asyncio.get_running_loop().run_in_executor(_executor, fn, *args)


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #

class UrlRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2048)


class ZipItem(BaseModel):
    filename: str = Field(..., max_length=200)
    markdown: str = ""


class ZipRequest(BaseModel):
    items: list[ZipItem] = Field(..., min_length=1, max_length=MAX_FILES)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/api/formats")
async def formats() -> dict[str, Any]:
    return {
        "extensions": SUPPORTED_EXTENSIONS,
        "max_file_mb": MAX_FILE_MB,
        "max_files": MAX_FILES,
        "url_fetch": ENABLE_URL_FETCH,
    }


@app.post("/api/convert")
async def convert(files: list[UploadFile] = File(...)) -> JSONResponse:
    if not files:
        raise HTTPException(400, "沒有收到檔案")
    if len(files) > MAX_FILES:
        raise HTTPException(413, f"一次最多 {MAX_FILES} 個檔案")

    async def one(upload: UploadFile) -> dict[str, Any]:
        source = upload.filename or "untitled"
        stem = safe_stem(source)
        try:
            data = await upload.read()
            if not data:
                raise ValueError("檔案是空的")
            if len(data) > MAX_FILE_BYTES:
                raise ValueError(f"超過單檔上限 {MAX_FILE_MB} MB")
            text = await _run(_convert_bytes, data, source)
            return {
                "source": source,
                "filename": f"{stem}.md",
                "markdown": text,
                "bytes": len(data),
                "chars": len(text),
                "ok": True,
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 — 單檔失敗不影響整批
            return {
                "source": source,
                "filename": f"{stem}.md",
                "markdown": "",
                "bytes": 0,
                "chars": 0,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}"[:400],
            }
        finally:
            await upload.close()

    results = await asyncio.gather(*(one(f) for f in files))
    return JSONResponse({"results": list(results)})


@app.post("/api/convert-url")
async def convert_url(payload: UrlRequest) -> JSONResponse:
    if not ENABLE_URL_FETCH:
        raise HTTPException(403, "此服務未開放 URL 轉換")
    url = payload.url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "只接受 http:// 或 https:// 開頭的網址")
    try:
        stem, text = await _run(_convert_uri, url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, f"{type(exc).__name__}: {exc}"[:400]) from exc
    return JSONResponse(
        {
            "results": [
                {
                    "source": url,
                    "filename": f"{stem}.md",
                    "markdown": text,
                    "bytes": 0,
                    "chars": len(text),
                    "ok": True,
                    "error": None,
                }
            ]
        }
    )


@app.post("/api/zip")
async def zip_results(payload: ZipRequest) -> StreamingResponse:
    buf = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in payload.items:
            name = f"{safe_stem(item.filename)}.md"
            counter = 1
            while name in used:
                name = f"{safe_stem(item.filename)}-{counter}.md"
                counter += 1
            used.add(name)
            zf.writestr(name, item.markdown)
    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="markdown-{stamp}.zip"'},
    )


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
