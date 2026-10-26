#!/usr/bin/env bash
# Демонстрація безпеки для відео (розділ 6). Усі спроби мають бути ВІДХИЛЕНІ.
# Запуск (з кореня репозиторію, система вже піднята): ./scripts/security_demo.sh
set -u
cd "$(dirname "$0")/.."
DC="docker compose exec -T mosquitto"
TLS="-h localhost -p 8883 --cafile /mosquitto/tls/ca.crt"

step() { printf '\n\033[1;36m=== %s\033[0m\n' "$1"; }

step "1. Підключення БЕЗ пароля (TLS є)"
$DC mosquitto_pub $TLS -t flat/test -m 1 2>&1 | tail -1

step "2. Підключення з НЕПРАВИЛЬНИМ паролем"
$DC mosquitto_pub $TLS -u openhab -P wrong -t flat/test -m 1 2>&1 | tail -1

step "3. Підключення БЕЗ TLS до порту 8883 (правильний пароль)"
$DC sh -c 'mosquitto_pub -h localhost -p 8883 -u admin -P "$MQTT_ADMIN_PASSWORD" -t flat/test -m 1' 2>&1 | tail -1

step "4. Порт 1883 (відкритий MQTT) — не слухає"
$DC mosquitto_pub -h localhost -p 1883 -t flat/test -m 1 2>&1 | tail -1

step "5. Сенсор температури намагається писати у ЧУЖИЙ топік (потужність)"
$DC sh -c "mosquitto_pub $TLS -u dev_temperature -P \"\$MQTT_PW_TEMPERATURE\" -q 1 -t flat/hallway/power/telemetry -m '{\"value\":99999}'"
echo "(публікація тихо відкинута ACL — перевіряємо значення потужності нижче)"

step "6. Сенсор руху намагається КЕРУВАТИ лампою"
$DC sh -c "mosquitto_pub $TLS -u dev_motion_living -P \"\$MQTT_PW_MOTION_LIVING\" -q 1 -t flat/livingroom/light/set -m '{\"brightness\":100}'"

step "7. Сенсор намагається ПІДПИСАТИСЬ на все (flat/#) — повинен отримати нічого"
$DC sh -c "timeout 4 mosquitto_sub $TLS -u dev_temperature -P \"\$MQTT_PW_TEMPERATURE\" -t 'flat/#' -v" || echo "(нічого не отримано за 4 с — ACL забороняє читання)"

step "8. Контроль: реальна потужність не змінилась (admin читає)"
$DC sh -c "mosquitto_sub $TLS -u admin -P \"\$MQTT_ADMIN_PASSWORD\" -t flat/hallway/power/telemetry -C 1 -W 6 -v"

step "9. Що записав брокер у лог (відмови)"
docker compose logs --no-log-prefix --tail 400 mosquitto 2>&1 \
  | grep -aEi "denied|not authori|bad user|alert unknown ca|TLS error|protocol error" | sort -u | cut -c1-180 | tail -12
