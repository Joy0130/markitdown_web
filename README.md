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

## 環境變數

`MAX_FILE_MB`(50) · `MAX_FILES`(20) · `CONVERT_WORKERS` · `UVICORN_WORKERS`(2) ·
`ENABLE_URL_FETCH`(1) · `MARKITDOWN_ENABLE_PLUGINS`(0) · `CERT_DIR`(/certs) · `CERT_FILE` · `KEY_FILE` · `KEY_PASSWORD`
