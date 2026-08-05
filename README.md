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