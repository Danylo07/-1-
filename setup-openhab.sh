#!/bin/sh
# Одноразова підготовка для openHAB (виконується в контейнері openhab-setup):
#  1) рендер mqtt.things із шаблону: пароль береться з .env і не потрапляє в git;
#  2) truststore для JVM = стандартні CA + наш CA (щоб openHAB довіряв брокеру по TLS).
set -e

: "${MQTT_OPENHAB_PASSWORD:?не задано MQTT_OPENHAB_PASSWORD}"

sed "s|@OPENHAB_MQTT_PASSWORD@|${MQTT_OPENHAB_PASSWORD}|g" \
    /templates/mqtt.things.template > /conf/things/mqtt.things
chmod 644 /conf/things/mqtt.things
echo "[setup] mqtt.things згенеровано"

JH="${JAVA_HOME:-$(dirname "$(dirname "$(readlink -f "$(which java)")")")}"
cp "$JH/lib/security/cacerts" /trust/truststore.jks
chmod u+w /trust/truststore.jks
keytool -importcert -noprompt -trustcacerts -alias iot-mc1-ca \
        -file /ca.crt -keystore /trust/truststore.jks -storepass changeit >/dev/null
chmod 644 /trust/truststore.jks
echo "[setup] truststore готовий (наш CA додано)"
