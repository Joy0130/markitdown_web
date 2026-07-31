#!/bin/sh
set -e

CERT_DIR="${CERT_DIR:-/certs}"
PORT="${PORT:-8443}"

pick() {
  for p in "$@"; do
    [ -f "$p" ] && { echo "$p"; return 0; }
  done
  return 0
}

if [ -z "$CERT_FILE" ]; then
  CERT_FILE="$(pick "$CERT_DIR/fullchain.pem" "$CERT_DIR/fullchain.cer" "$CERT_DIR/cert.pem" \
                    "$CERT_DIR/server.crt" "$CERT_DIR"/*.crt "$CERT_DIR"/*.cer)"
fi
if [ -z "$KEY_FILE" ]; then
  KEY_FILE="$(pick "$CERT_DIR/privkey.pem" "$CERT_DIR/private.key" "$CERT_DIR/key.pem" \
                   "$CERT_DIR/server.key" "$CERT_DIR"/*.key)"
fi

set -- uvicorn app:app --host 0.0.0.0 --port "$PORT" \
      --workers "${UVICORN_WORKERS:-2}" --proxy-headers --forwarded-allow-ips='*'

if [ -n "$CERT_FILE" ] && [ -n "$KEY_FILE" ]; then
  echo "[boot] HTTPS  cert=$CERT_FILE  key=$KEY_FILE"
  set -- "$@" --ssl-certfile "$CERT_FILE" --ssl-keyfile "$KEY_FILE"
  [ -n "$KEY_PASSWORD" ] && set -- "$@" --ssl-keyfile-password "$KEY_PASSWORD"
else
  echo "[boot] HTTP only — $CERT_DIR 內找不到憑證"
fi

exec "$@"
