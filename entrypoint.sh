#!/bin/sh
# Готує файл паролів із змінних середовища (.env) та копіює TLS-файли
# з правильними правами, після чого запускає брокер.
# Паролі НЕ зберігаються в репозиторії: хеші генеруються при кожному старті.
set -e

PW=/mosquitto/data/passwd
: > "$PW"

mosquitto_passwd -b "$PW" admin   "$MQTT_ADMIN_PASSWORD"
mosquitto_passwd -b "$PW" openhab "$MQTT_OPENHAB_PASSWORD"

for key in motion_living motion_bedroom temperature humidity lux power \
           light_living light_bedroom heater; do
  upper=$(echo "$key" | tr 'a-z' 'A-Z')
  eval "pass=\$MQTT_PW_${upper}"
  if [ -z "$pass" ]; then
    echo "ПОМИЛКА: не задано MQTT_PW_${upper} у .env" >&2
    exit 1
  fi
  mosquitto_passwd -b "$PW" "dev_${key}" "$pass"
done

cp /mosquitto/config/acl /mosquitto/data/acl   # копія з правильним власником
mkdir -p /mosquitto/tls
cp /certs/ca.crt /certs/server.crt /certs/server.key /mosquitto/tls/
chown -R mosquitto:mosquitto /mosquitto/tls /mosquitto/data
chmod 600 /mosquitto/tls/server.key "$PW" /mosquitto/data/acl

exec "$@"
