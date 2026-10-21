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
import http.client
import io
import ipaddress
import json
import os
import re
import socket
import ssl
import tempfile
import unicodedata
import urllib.parse
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
from lxml import etree
from markitdown import MarkItDown
import pymupdf4llm

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
        ".html", ".htm", ".xml", ".json", ".txt",
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


MAX_REDIRECTS = 5
FETCH_TIMEOUT = int(os.getenv("URL_FETCH_TIMEOUT", "30"))

# 不少站台（Cloudflare 等）會直接 403 掉沒有 User-Agent 的請求，
# 因此送出一組一般瀏覽器的標頭。Accept-Encoding 固定 identity，
# 我們沒有解壓縮邏輯，不能讓對方回 gzip。
USER_AGENT = os.getenv(
    "URL_FETCH_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)
_FETCH_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",
}

# Content-Type → 副檔名，抓回來的內容沒有可用副檔名時據此判斷 converter
_CONTENT_TYPE_EXT = {
    "application/pdf": ".pdf",
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/tab-separated-values": ".tsv",
    "application/json": ".json",
    "text/xml": ".xml",
    "application/xml": ".xml",
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}


def _check_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    """只要落在非公開網段就拒絕。"""
    # ::ffff:127.0.0.1 這類 IPv4-mapped 位址要拆出內層再判斷
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise ValueError(
            f"URL 解析到內部位址 {ip}，基於安全考量已封鎖。"
            f"只允許存取公開網際網路位址。"
        )


def _resolve_and_validate(url: str) -> tuple[urllib.parse.ParseResult, list[str], int]:
    """解析 URL、驗證所有 DNS 結果，並回傳可實際連線的 IP 清單。

    關鍵：DNS 只解析這一次，之後直接連線到這裡回傳的 IP，
    請求階段不再做第二次解析，因此無法用 DNS Rebinding 繞過檢查。
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("只接受 http:// 或 https:// 開頭的網址")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("無法從 URL 解析出主機名稱")
    if hostname.lower().rstrip(".") == "localhost":
        raise ValueError("不允許存取 localhost")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        addr_infos = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"DNS 解析失敗：{hostname}") from exc
    if not addr_infos:
        raise ValueError(f"DNS 解析失敗：{hostname}")

    # 全部檢查過才放行，避免「有一筆是內網」的混合回應
    ips: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in addr_infos:
        _check_ip(ipaddress.ip_address(sockaddr[0]))
        if sockaddr[0] not in ips:
            ips.append(sockaddr[0])

    return parsed, ips, port


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """連線到已驗證過的 IP，但 Host 標頭仍用原主機名稱。"""

    def __init__(self, host: str, ips: list[str], port: int, timeout: int) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ips = ips

    def connect(self) -> None:  # pragma: no cover - 交由 socket 層處理
        last: Exception | None = None
        for ip in self._pinned_ips:  # 依序嘗試，避開不可達的 IPv6/IPv4
            try:
                self.sock = socket.create_connection((ip, self.port), self.timeout)
                return
            except OSError as exc:
                last = exc
        raise last or OSError("無法建立連線")


class _PinnedHTTPSConnection(_PinnedHTTPConnection):
    """同上，並以原主機名稱做 SNI 與憑證驗證。"""

    default_port = 443

    def connect(self) -> None:  # pragma: no cover - 交由 socket 層處理
        super().connect()
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(self.sock, server_hostname=self.host)


def _fetch_once(url: str, max_bytes: int) -> tuple[int, dict[str, str], bytes]:
    """對單一 URL 發出一次 GET（不跟隨轉址），連線鎖定在已驗證的 IP 上。"""
    parsed, ips, port = _resolve_and_validate(url)
    conn_cls = _PinnedHTTPSConnection if parsed.scheme == "https" else _PinnedHTTPConnection
    conn = conn_cls(parsed.hostname, ips, port, FETCH_TIMEOUT)
    try:
        path = urllib.parse.urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
        conn.request("GET", path, headers={"Host": parsed.netloc, **_FETCH_HEADERS})
        resp = conn.getresponse()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        body = b"" if 300 <= resp.status < 400 else resp.read(max_bytes + 1)
        return resp.status, headers, body
    finally:
        conn.close()


def _safe_fetch(url: str, max_bytes: int) -> tuple[str, str, bytes]:
    """安全抓取 URL 內容，回傳 (最終網址, content-type, 內容)。

    每一次轉址都會重新走一遍 `_resolve_and_validate`，
    所以攻擊者無法用 302 轉址把請求導到內網。
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        status, headers, body = _fetch_once(current, max_bytes)
        if 300 <= status < 400 and headers.get("location"):
            current = urllib.parse.urljoin(current, headers["location"])
            continue
        if status >= 400:
            hint = "（來源站台拒絕存取，可能有反爬蟲或登入限制）" if status in (401, 403, 429) else ""
            raise ValueError(f"下載失敗，HTTP {status}{hint}")
        if len(body) > max_bytes:
            raise ValueError(f"超過單檔上限 {MAX_FILE_MB} MB")
        content_type = headers.get("content-type", "").split(";")[0].strip().lower()
        return current, content_type, body
    raise ValueError(f"轉址次數超過上限（{MAX_REDIRECTS} 次）")


def _convert_pdf(tmp_path: str) -> str:
    """PDF 用 pymupdf4llm：依字型大小/粗體推斷標題階層，輸出結構化 markdown。"""
    return pymupdf4llm.to_markdown(tmp_path) or ""


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WQ = f"{{{_W}}}"
_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
_ARTICLE_RE = re.compile(r"^第\s*[一二三四五六七八九十百千零〇\d]+\s*([章條节節])")
_DOCX_STYLE_MAP = "\n".join(
    f"p[style-name='MdHeading{n}'] => h{n}:fresh" for n in (1, 2, 3)
)


def _docx_style_sizes(styles: etree._Element) -> tuple[dict[str, str], dict[str, int | None], int]:
    """回傳 (styleId→名稱, styleId→字級[半點，沿 basedOn 繼承], 預設字級)。"""
    default = styles.find(f"{_WQ}docDefaults/{_WQ}rPrDefault/{_WQ}rPr/{_WQ}sz")
    default_sz = int(default.get(f"{_WQ}val")) if default is not None else 20
    names: dict[str, str] = {}
    own: dict[str, int | None] = {}
    parent: dict[str, str] = {}
    for st in styles.iter(f"{_WQ}style"):
        sid = st.get(f"{_WQ}styleId")
        name = st.find(f"{_WQ}name")
        names[sid] = (name.get(f"{_WQ}val") if name is not None else "").lower()
        sz = st.find(f"{_WQ}rPr/{_WQ}sz")
        own[sid] = int(sz.get(f"{_WQ}val")) if sz is not None else None
        based = st.find(f"{_WQ}basedOn")
        if based is not None:
            parent[sid] = based.get(f"{_WQ}val")

    def resolve(sid: str, depth: int = 0) -> int | None:
        if sid not in own or depth > 20:
            return None
        if own[sid] is not None:
            return own[sid]
        return resolve(parent[sid], depth + 1) if sid in parent else None

    return names, {sid: resolve(sid) for sid in own}, default_sz


def _infer_docx_headings(data: bytes) -> tuple[bytes, str | None]:
    """很多 Word 檔的標題只是「放大字」而非套用標題樣式，mammoth 因此全當成內文。
    這裡依字級 / 粗體 / 「第 X 條」等線索推斷標題，套上自訂樣式再交給 MarkItDown。
    文件本身已有標題樣式時不動它。"""
    with zipfile.ZipFile(io.BytesIO(data)) as zin:
        doc = etree.fromstring(zin.read("word/document.xml"), _XML_PARSER)
        styles = etree.fromstring(zin.read("word/styles.xml"), _XML_PARSER)
        names, style_sz, default_sz = _docx_style_sizes(styles)
        normal = next((sid for sid, n in names.items() if n == "normal"), None)
        base_sz = style_sz.get(normal) or default_sz if normal else default_sz

        body = doc.find(f"{_WQ}body")
        paras = []
        for p in body.iterfind(f"{_WQ}p"):  # 只看本文段落，表格內不處理
            ps = p.find(f"{_WQ}pPr/{_WQ}pStyle")
            sid = ps.get(f"{_WQ}val") if ps is not None else normal
            if names.get(sid, "").startswith(("heading", "title")):
                return data, None  # 已有真正的標題樣式
            p_sz = style_sz.get(sid) or base_sz
            runs = []
            for r in p.iter(f"{_WQ}r"):
                text = "".join(t.text or "" for t in r.iter(f"{_WQ}t"))
                if not text.strip():
                    continue
                sz = r.find(f"{_WQ}rPr/{_WQ}sz")
                b = r.find(f"{_WQ}rPr/{_WQ}b")
                bold = b is not None and b.get(f"{_WQ}val", "1") not in ("0", "false")
                runs.append((text, int(sz.get(f"{_WQ}val")) if sz is not None else p_sz, bold))
            if runs:
                paras.append((p, runs))

        if not paras:
            return data, None
        # 內文字級 = 以字數加權的眾數
        weight: dict[int, int] = {}
        for _, runs in paras:
            for text, sz, _ in runs:
                weight[sz] = weight.get(sz, 0) + len(text.strip())
        body_sz = max(weight, key=weight.get)
        has_chapter = any(
            (m := _ARTICLE_RE.match("".join(t for t, _, _ in runs).strip())) and m.group(1) == "章"
            for _, runs in paras
        )

        marked = 0
        for p, runs in paras:
            text = "".join(t for t, _, _ in runs).strip()
            if len(text) > 80 or p.find(f"{_WQ}pPr/{_WQ}numPr") is not None:
                continue
            max_sz = max(sz for _, sz, _ in runs)
            all_bold = all(b for _, _, b in runs)
            level = None
            if max_sz >= body_sz * 1.5:
                level = 1
            elif max_sz >= body_sz * 1.2 and all_bold:  # 只放大不加粗常是簽名欄等表單欄位
                level = 2
            elif m := _ARTICLE_RE.match(text):
                level = 2 if m.group(1) == "章" or not has_chapter else 3
            elif all_bold and len(text) <= 30 and not text.endswith(("。", "，", "；")):
                level = 3
            if level is None:
                continue
            ppr = p.find(f"{_WQ}pPr")
            if ppr is None:
                ppr = etree.SubElement(p, f"{_WQ}pPr")
                p.insert(0, ppr)
            ps = ppr.find(f"{_WQ}pStyle")
            if ps is None:
                ps = etree.Element(f"{_WQ}pStyle")
                ppr.insert(0, ps)
            ps.set(f"{_WQ}val", f"MdHeading{level}")
            marked += 1

        if not marked:
            return data, None
        for n in (1, 2, 3):
            st = etree.SubElement(styles, f"{_WQ}style", {f"{_WQ}type": "paragraph", f"{_WQ}styleId": f"MdHeading{n}"})
            etree.SubElement(st, f"{_WQ}name", {f"{_WQ}val": f"MdHeading{n}"})

        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename == "word/document.xml":
                    zout.writestr(item, etree.tostring(doc, xml_declaration=True, encoding="UTF-8", standalone=True))
                elif item.filename == "word/styles.xml":
                    zout.writestr(item, etree.tostring(styles, xml_declaration=True, encoding="UTF-8", standalone=True))
                else:
                    zout.writestr(item, zin.read(item.filename))
    return out.getvalue(), _DOCX_STYLE_MAP


def _convert_json(data: bytes) -> str:
    """MarkItDown 把 JSON 當純文字原封輸出，這裡改成 pretty-print 後包 code fence。"""
    text = data.decode("utf-8-sig", errors="replace")
    try:
        text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except ValueError:
        text = text.strip()  # 不是合法 JSON 就照原文包起來
    # 內容本身含有 ``` 時，外層 fence 要比它更長才不會提前結束
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}json\n{text}\n{fence}"


def _convert_with_suffix(data: bytes, suffix: str) -> tuple[str | None, str]:
    """MarkItDown 依副檔名挑選 converter，因此寫入保留副檔名的暫存檔。"""
    if suffix == ".json":
        return None, _convert_json(data)
    kwargs: dict[str, Any] = {}
    if suffix == ".docx":
        try:
            data, style_map = _infer_docx_headings(data)
        except (KeyError, zipfile.BadZipFile, etree.XMLSyntaxError, ValueError):
            style_map = None  # 推斷失敗就照原檔轉
        if style_map:
            kwargs["style_map"] = style_map
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        if suffix == ".pdf":
            return None, _convert_pdf(tmp_path)
        result = _new_converter().convert(tmp_path, **kwargs)
        return getattr(result, "title", None), (result.text_content or "")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _convert_bytes(data: bytes, filename: str) -> str:
    return _convert_with_suffix(data, Path(filename).suffix.lower())[1]


def _convert_uri(uri: str) -> tuple[str, str]:
    """先用鎖定 IP 的方式把內容抓下來，再本地轉換；轉換器不會自己連網。"""
    final_url, content_type, data = _safe_fetch(uri, MAX_FILE_BYTES)
    if not data:
        raise ValueError("下載到的內容是空的")

    path = urllib.parse.urlparse(final_url).path
    stem = safe_stem(Path(path).stem or "url")
    suffix = Path(path).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        suffix = _CONTENT_TYPE_EXT.get(content_type, ".html")

    title, text = _convert_with_suffix(data, suffix)
    return safe_stem(title or stem), text


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
            ext = Path(source).suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS:
                allowed = ", ".join(SUPPORTED_EXTENSIONS)
                raise ValueError(
                    f"不支援的檔案格式「{ext or '(無副檔名)'}」，"
                    f"僅接受：{allowed}"
                )
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
