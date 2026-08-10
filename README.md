# MarkItDown Web

以 MarkItDown Python API 為核心的文件轉 Markdown 網頁服務。

## 部署到 joylinux

```bash
scp -r markitdown-web joy@joylinux:~/workspaces/
ssh joy@joylinux
cd ~/workspaces/markitdown-web
docker compose up -d --build
docker compose logs -f          # 確認 [boot] HTTPS 那行有讀到憑證
```

開啟 `https://joylinux.futsu.com.tw:9444/`

## 憑證

`/home/joy/workspaces/certs/cert2026-1` 以唯讀掛載到容器的 `/certs`。
entrypoint 會依序尋找 `fullchain.pem` → `cert.pem` → `*.crt`／`privkey.pem` → `key.pem` → `*.key`。
檔名不同就在 compose 的 `CERT_FILE` / `KEY_FILE` 直接指定。
私鑰需容器內 uid 10001 可讀：`chmod 644` 憑證、`chmod 640` 並確認群組，或直接 `chmod 644` 私鑰。

## API

| Method | Path | 說明 |
| --- | --- | --- |
| POST | `/api/convert` | multipart `files`，批次轉換 |
| POST | `/api/convert-url` | `{"url": "https://…"}` |
| POST | `/api/zip` | 把結果打包成 zip |
| GET | `/api/formats` | 支援副檔名與上限 |
| GET | `/healthz` | 健康檢查 |
| GET | `/api/docs` | OpenAPI |

## 文件處理流程

使用者透過網頁上傳的檔案，全程在容器內部完成處理，**不會傳送到任何外部伺服器**。

### 處理生命週期

```
使用者上傳 ──→ FastAPI 接收到記憶體 ──→ 寫入 /tmp 暫存檔（tmpfs · RAM）
                                              │
                                              ▼
                                    MarkItDown / pymupdf4llm
                                        本地轉換
                                              │
                                              ▼
                                    os.unlink() 立即刪除暫存檔
                                              │
                                              ▼
                                    回傳 Markdown 純文字給前端
```

### 各階段詳細說明

| 階段 | 位置 | 說明 |
| --- | --- | --- |
| 上傳接收 | FastAPI `UploadFile` | 檔案以 multipart 形式送到 `POST /api/convert`，讀入記憶體 (`await upload.read()`) |
| 暫存寫入 | Container `/tmp` | `tempfile.NamedTemporaryFile(suffix=...)` 寫入帶原始副檔名的暫存檔，供轉換器辨識格式 |
| 格式轉換 | 本地函式庫 | PDF 使用 `pymupdf4llm`；其餘格式使用 `MarkItDown`。兩者皆為離線運算，不連線外部服務 |
| 暫存刪除 | `finally` 區塊 | 無論成功或失敗，`os.unlink(tmp_path)` 在 `finally` 中執行，確保暫存檔被刪除 |
| 結果回傳 | JSON Response | 純文字 Markdown 透過 HTTPS 回傳前端，前端在瀏覽器記憶體中呈現 |

### 資料安全性

- **不寫入磁碟** — `/tmp` 掛載為 `tmpfs`（`docker-compose.yml` 中 `tmpfs: /tmp:size=1g`），資料僅存在記憶體
- **不持久化** — 轉換完成後立即刪除暫存檔；即使未刪成功，容器重啟後 tmpfs 也會清空
- **無資料庫** — 沒有任何資料庫或持久儲存用來保存上傳內容或轉換結果
- **無外部呼叫** — `markitdown` 與 `pymupdf4llm` 皆為離線函式庫，不含遙測或雲端通訊
- **插件已關閉** — `MARKITDOWN_ENABLE_PLUGINS=0`，不會載入可能有網路行為的第三方插件
- **非 root 執行** — 容器以 `appuser`（uid 10001）執行，降低安全風險
- **HTTPS 加密** — 透過 TLS 憑證加密傳輸，防止中間人攔截
- **無 Volume 掛出** — 唯一的 volume 是 `/certs:ro`（唯讀憑證），沒有將 `/tmp` 或其他目錄掛載到宿主機

> **結論：上傳的文件只在容器記憶體中短暫存在（轉換期間），轉換完即刪除，不留任何痕跡，不會外流。**

### URL 抓取功能與 SSRF 防護

網頁提供「貼上 URL 直接轉換」的功能（由 `ENABLE_URL_FETCH` 控制）。此功能的 HTTP 請求是**由伺服器端（VM）發起**，而非使用者的瀏覽器。

#### 為什麼需要防護？

因為 VM 本身位於公司內網中，可存取到其他內部伺服器、資料庫管理介面、監控系統等。如果不加驗證，任何使用網頁的人都可以透過此功能，間接「借用」VM 的網路身份存取不該碰的內部資源——這就是 **SSRF（Server-Side Request Forgery，伺服器端請求偽造）** 攻擊。

```
使用者的電腦                        公司 Linux VM（markitdown-web）
     │                                       │
     │  輸入: http://192.168.1.100/機密文件     │
     │ ────────────────────────────────────→  │
     │                                       │── VM 用自己的身份去存取 192.168.1.100
     │                                       │  （VM 在內網中，所以連得到）
     │       ← 回傳內容給使用者 ────────────────│
```

#### 防護機制

`app.py` 中的 `_validate_url()` 在 URL 被實際請求**之前**，先透過 DNS 解析取得目標 IP，再驗證該 IP 是否為內網或保留位址。即使攻擊者使用自訂域名（如 `http://my-trick.com`）但 DNS 指向內網 IP，也一樣會被擋下。

#### 封鎖範圍

| 使用者輸入的 URL | 結果 | 原因 |
| --- | --- | --- |
| `http://192.168.x.x/...` | ❌ 封鎖 | 私有 IP（`is_private`） |
| `http://10.0.0.x/...` | ❌ 封鎖 | 私有 IP（`is_private`） |
| `http://172.16.x.x/...` | ❌ 封鎖 | 私有 IP（`is_private`） |
| `http://localhost/...` | ❌ 封鎖 | 迴路位址（`is_loopback`） |
| `http://169.254.x.x/...` | ❌ 封鎖 | 鏈路本地（`is_link_local`） |
| `http://intranet.company.local/...` | ❌ 封鎖 | DNS 解析後 IP 仍為內網位址 |
| `https://www.google.com` | ✅ 允許 | 公開 IP |
| `https://zh.wikipedia.org/...` | ✅ 允許 | 公開 IP |

> 若需完全禁止出站流量，可在 `docker-compose.yml` 設定 `ENABLE_URL_FETCH: "0"` 關閉整個 URL 抓取功能。


# MCP 部署設定


## 先天限制：無法在 Claude 直接設定
 
- claude.ai 網頁版的 **Add custom connector** 對話框，只有 Name、Remote MCP server URL、OAuth Client ID/Secret 這幾個欄位 — 沒有填 Bearer Token 的欄位。
- **沒有 header 認證欄位**：Bearer token 的 header 認證是 beta 功能，需申請權限才會出現對應欄位。
- **沒有 OAuth**：markitdown-mcp 本身沒實作 OAuth，只能用 Nginx 來實作解決 Bearer token。
- **自簽憑證問題**：即使解決了驗證方式，claude.ai 雲端伺服器對你的自簽憑證也會判定不受信任而拒絕連線。


 
## 先天限制：只能在 Desktop 版本上使用 markitdown
 
原因：markitdown 是標示為「Local dev」的本機連接器——它是跑在電腦上的本地 MCP server（透過 stdio 或本機端口連線）。
 
- **Desktop 版**：可以直接存取本機行程/檔案系統，所以能連到跑在 localhost 或本機路徑的 MCP server。
- **Web 版**：在瀏覽器沙盒中執行，無法連線到 localhost 或本機行程，因此不會顯示、也無法新增這類 Local dev 連接器。


## Claude 後端設定：claude_desktop_config.json 檔案位置
 
如安裝時為預設路徑，可以直接開啟：
 
**macOS**
```
~/Library/Application Support/Claude/claude_desktop_config.json
```
 
**Windows**
```
%APPDATA%\Claude\claude_desktop_config.json
```
 
**Linux**
```
~/.config/Claude/claude_desktop_config.json
```
 
**macOS 開啟資料夾**
```
open ~/Library/Application\ Support/Claude/
```

如果都找不到設定檔路徑
> 進入 Claude desktop settings > Developer > Local MCP servers > Edit Config。

---
 
## 編輯設定檔
 
沒有裝 VSCode 請用記事本開啟 `claude_desktop_config.json` 檔案，在最底下貼上下方程式碼。
 
> 注意 `{ }` 和 `,`
 
```bash
},
        "mcpServers": {
        "markitdown": {
            "command": "npx",
            "args": [
            "-y",
            "mcp-remote",
            "https://<自己的網址>/mcp/",
            "--header",
            "Authorization: Bearer <自己的 token>",
            "--transport",
            "http-only"
            ]
        }
    }
}
```
 
---
 
## 儲存並重啟
 
設定後存檔，關閉 Claude Desktop 應用程式。
 
- **Windows**：點 x 後請到右下方找到 Claude 圖示按右鍵 close 才算真正關閉。
- **Mac**：開啟應用程式按 `cmd + q` 退出應用程式。

## 確認有成功
- 進入 Claude desktop settings > Developer > Local MCP servers 看到狀態變成 Running
- 在左側工具列看到MCP工具中有顯示markitdown且開關為開啟的狀態，表示成功

---
## 連線共用磁碟機
 
**mac**：Finder → 前往 → 連接伺服器（⌘K），輸入：
```
smb://[伺服器的IP_ADDRESS]/markitdown-docs
```
 
**windows**：新增網路磁碟機（磁碟機代碼都可以）：
```
\\<[伺服器的IP_ADDRESS]>\markitdown-docs
```
輸入連線磁碟機帳號/密碼 (自行使用smbd建立的連線帳號密碼)

## samba安裝方式(Linux)

```bash
sudo apt update && sudo apt install -y samba
```

---
 
## 實際操作
 
到 Claude desktop chat 中輸入：
 
> 幫我把 /workdir/檔名.pdf 轉成 markdown
 
但是每次都要帶路徑太麻煩了，所以……
 
> 到 Claude desktop 設定 > General > Instructions for Claude，貼上下方提示詞：
 
```bash
檔案轉換：當我說「幫我轉換 <檔名>」時，請直接呼叫已連接的
markitdown 工具（convert_to_markdown），路徑固定使用
file:///workdir/<檔名>。轉換完成後直接輸出成 .md 檔案，
不需要詢問我是否要輸出，也不需要先摘要內容。
```
 
設定後，之後每次詢問只要輸入
> 幫我轉換 <檔名>，例如：「幫我轉換 test.pdf」。
 
Claude 就會自動調用 markitdown MCP 工具，轉換過程不會佔用到 token 使用量。成功後會提供 md 檔案，可以直接下載到本機。
 
---

# ChatGPT 設定

## Claude 設定後會自動連動 ChatGPT Codex
 
> 設定 > 外掛程式 > MCP

---

## 如果 Claude 沒有設定或是第一次要在 Codex 中設定

> 設定 > 外掛程式 > MCP > Add > Add MCP server

啟動指令 (引數 · 一列一個)

```bash
npx
-y
mcp-remote
https://<自己的網址>/mcp/
--header
Authorization: Bearer <自己的 token>
--transport
http-only
```

對應欄位填入資料後儲存。儲存後重新開啟 Codex 應用程式，回到 外掛程式 > MCP 確認 markitdown 已出現在伺服器清單中。

---