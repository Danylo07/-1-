#!/usr/bin/env bash
# Перший запуск: створює .env з випадковими паролями та генерує сертифікати.
# Після цього: docker compose up -d
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "[init] Створюю .env з випадковими паролями"
  while IFS= read -r line; do
    if [[ "$line" == *CHANGE_ME* ]]; then
      echo "${line/CHANGE_ME/$(openssl rand -hex 12)}"
    else
      echo "$line"
    fi
  done < .env.example > .env
  chmod 600 .env
else
  echo "[init] .env уже існує — не чіпаю"
fi

./certs/generate.sh
echo "[init] Готово. Тепер: docker compose up -d"
