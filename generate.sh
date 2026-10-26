#!/usr/bin/env bash
# Генерація власного CA та серверного сертифіката для Mosquitto (TLS, порт 8883).
# Використання: ./certs/generate.sh [--force]
# Приватні ключі (*.key) в .gitignore і НЕ потрапляють у репозиторій.
set -euo pipefail
cd "$(dirname "$0")"

if [[ -f ca.crt && -f server.crt && -f server.key && "${1:-}" != "--force" ]]; then
  echo "[certs] Сертифікати вже існують (використайте --force, щоб перегенерувати)."
  exit 0
fi

rm -f ca.* server.* *.srl

echo "[certs] 1/3 Власний кореневий CA (RSA 4096, 10 років)"
openssl genrsa -out ca.key 4096 2>/dev/null
openssl req -x509 -new -key ca.key -sha256 -days 3650 \
  -subj "/O=IoT MC1/CN=IoT MC1 Local CA" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -out ca.crt

echo "[certs] 2/3 Ключ і запит серверного сертифіката"
openssl genrsa -out server.key 2048 2>/dev/null
openssl req -new -key server.key -subj "/O=IoT MC1/CN=mosquitto" -out server.csr

# SAN: ім'я сервісу в docker-мережі + localhost (для mosquitto_sub з хоста)
cat > server.cnf <<CNF
basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:mosquitto,DNS:localhost,IP:127.0.0.1
CNF

echo "[certs] 3/3 Підпис серверного сертифіката нашим CA (825 днів)"
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out server.crt -days 825 -sha256 -extfile server.cnf 2>/dev/null

rm -f server.csr server.cnf ca.srl
chmod 600 ca.key server.key
chmod 644 ca.crt server.crt
echo "[certs] Готово: ca.crt, server.crt, server.key (ca.key зберігайте лише локально)"
