#!/usr/bin/env python3
"""
Симулятор пристроїв «Енергоощадна розумна квартира» (варіант 1).

Кожен пристрій — окремий MQTT-клієнт зі СВОЇМ користувачем, паролем, Last Will
і статусом online/offline (retain). Усі клієнти ходять на брокер лише через TLS.

Дані не random(0, 100): пристрої читають спільну фізичну модель квартири (World):
  * температура: теплові втрати до вулиці + нагрівання обігрівачем з гістерезисом;
  * освітленість: денне світло (залежить від години й хмарності) + внесок штучного світла;
  * потужність: базове навантаження (холодильник) + світло + обігрівач + шум;
  * присутність: марковський процес (вітальня / спальня / нікого вдома).

Сценарії (для демонстрації кожного правила openHAB):
  python devices.py --scenario dark_motion   # темно + рух у вітальні   -> правило 1
  python devices.py --scenario away          # нікого вдома             -> правило 2 (Економ)
  python devices.py --scenario return        # з'явився рух             -> правило 2 (21 °C)
  python devices.py --scenario overload      # >2500 Вт                 -> правило 3
  python devices.py --scenario demo          # усі по черзі (для відео)
Змінити сценарій у вже запущеному симуляторі (docker compose):
  docker compose exec simulator python devices.py --trigger overload
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import signal
import ssl
import sys
import threading
import time

import paho.mqtt.client as mqtt

# ----------------------------------------------------------------------------
# Налаштування (з оточення; паролі приходять з .env через docker compose)
# ----------------------------------------------------------------------------
OBJ = "flat"
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "8883"))
MQTT_CA = os.getenv("MQTT_CA", "/certs/ca.crt")
TZ_OFFSET_H = float(os.getenv("SIM_TZ_OFFSET", "2"))
CTL_FIFO = os.getenv("SIM_CTL_FIFO", "/tmp/sim_ctl")

# QoS: телеметрія — 0 (часта, втрата одного відліку не критична, наступний прийде за секунди);
#      команди й стани — 1 (доставка «принаймні раз», команда не може загубитись; повтор
#      безпечний, бо команди ідемпотентні: «яскравість = 70»).
QOS_TELEMETRY = 0
QOS_COMMAND = 1
QOS_STATE = 1
QOS_STATUS = 1

SCENARIOS = ["normal", "dark_motion", "away", "return", "overload", "demo"]


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def local_hour() -> float:
    t = time.gmtime(time.time() + TZ_OFFSET_H * 3600)
    return t.tm_hour + t.tm_min / 60 + t.tm_sec / 3600


# ----------------------------------------------------------------------------
# Фізична модель квартири
# ----------------------------------------------------------------------------
class World:
    HEATER_POWER_W = 1800.0
    HEATER_RATE = 0.004      # °C/с нагрів при роботі обігрівача
    LEAK_TAU = 10800.0       # с, стала часу тепловтрат (3 год)

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.temp = 21.0
        self.out_temp = 4.0
        self.hum = 45.0
        self.cloud = random.uniform(0.6, 1.0)
        self.lights = {"living": 0, "bedroom": 0}      # яскравість 0..100
        self.heater = {"on": True, "setpoint": 21.0, "heating": False}
        self.extra_load_w = 0.0                         # сценарій «overload»
        self.daylight_override = None                   # сценарій «dark_motion»
        self.forced_presence = None                     # сценарії away/return/dark_motion
        self.location = random.choice(["living", "bedroom"])
        self.presence = {"living": False, "bedroom": False}
        self.scenario = "normal"

    # --- сценарії ---------------------------------------------------------
    def set_scenario(self, name: str) -> None:
        with self.lock:
            if name == "normal":
                self.forced_presence = None
                self.daylight_override = None
                self.extra_load_w = 0.0
            elif name == "dark_motion":
                self.daylight_override = 5.0
                self.forced_presence = {"living": True, "bedroom": False}
            elif name == "away":
                self.forced_presence = {"living": False, "bedroom": False}
            elif name == "return":
                self.forced_presence = {"living": True, "bedroom": False}
            elif name == "overload":
                # обігрівач гріє на повну + прилади -> понад 2500 Вт
                self.heater.update(on=True, setpoint=26.0)
                self.extra_load_w = 1000.0
            else:
                raise ValueError(name)
            self.scenario = name
        log(f"[сценарій] {name}")

    # --- допоміжні обчислення ---------------------------------------------
    def daylight_indoor_lux(self) -> float:
        if self.daylight_override is not None:
            return self.daylight_override
        h = local_hour()
        if 7 < h < 17:
            return 600 * math.sin(math.pi * (h - 7) / 10) * self.cloud * 0.3
        return 0.0

    def light_lux(self) -> float:
        # датчик освітленості у коридорі «бачить» лише частку штучного світла
        return 0.2 * (120 * self.lights["living"] + 60 * self.lights["bedroom"]) / 100

    def power_w(self) -> float:
        t = time.time()
        fridge = 80.0 if (t % 1200) < 400 else 0.0       # компресор холодильника
        p = 120.0 + fridge
        p += 45.0 * self.lights["living"] / 100 + 25.0 * self.lights["bedroom"] / 100
        p += self.HEATER_POWER_W if self.heater["heating"] else 0.0
        p += self.extra_load_w
        return max(0.0, p + random.gauss(0, 12))

    # --- крок моделі -------------------------------------------------------
    def step(self, dt: float) -> None:
        with self.lock:
            hour = local_hour()
            self.out_temp = 4 + 3 * math.sin(2 * math.pi * (hour - 9) / 24)

            # присутність
            if self.forced_presence is not None:
                self.presence = dict(self.forced_presence)
            else:
                if random.random() < dt / 240.0:
                    self.location = random.choices(["living", "bedroom", "away"], weights=[5, 3, 2])[0]
                self.presence = {"living": self.location == "living",
                                 "bedroom": self.location == "bedroom"}

            # термостат з гістерезисом ±0.3 °C
            h = self.heater
            if h["on"]:
                if self.temp < h["setpoint"] - 0.3:
                    h["heating"] = True
                elif self.temp > h["setpoint"] + 0.3:
                    h["heating"] = False
            else:
                h["heating"] = False

            # теплообмін
            d_temp = -(self.temp - self.out_temp) * dt / self.LEAK_TAU
            if h["heating"]:
                d_temp += self.HEATER_RATE * dt
            if any(self.presence.values()):
                d_temp += 0.0002 * dt
            self.temp += d_temp

            # вологість: повільно прагне до значення, що залежить від температури
            target_h = 45 + (21 - self.temp) * 1.5
            self.hum += (target_h - self.hum) * dt / 600 + random.gauss(0, 0.04)
            self.hum = clamp(self.hum, 25, 75)

            # хмарність — плавний випадковий дрейф
            self.cloud = clamp(self.cloud + random.gauss(0, 0.01), 0.5, 1.0)


# ----------------------------------------------------------------------------
# Базовий пристрій: власний MQTT-клієнт, TLS, Last Will, статус online (retain)
# ----------------------------------------------------------------------------
class Device:
    is_actuator = False

    def __init__(self, key: str, zone: str, name: str, world: World) -> None:
        self.key, self.zone, self.name, self.world = key, zone, name, world
        self.user = f"dev_{key}"
        self.password = os.getenv(f"MQTT_PW_{key.upper()}", "")
        self.base = f"{OBJ}/{zone}/{name}"
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f"sim-{key}", protocol=mqtt.MQTTv311)
        self.connected = False

    def topic(self, leaf: str) -> str:
        return f"{self.base}/{leaf}"

    # --- підключення -------------------------------------------------------
    def start(self) -> None:
        c = self.client
        c.username_pw_set(self.user, self.password)
        c.tls_set(ca_certs=MQTT_CA)                       # перевірка сертифіката й імені хоста
        # Last Will: якщо процес «впаде» без DISCONNECT, брокер сам опублікує offline (retain)
        c.will_set(self.topic("status"), "offline", qos=QOS_STATUS, retain=True)
        c.reconnect_delay_set(min_delay=1, max_delay=10)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
        c.loop_start()

    def stop(self) -> None:
        # Штатне завершення: LWT при коректному DISCONNECT НЕ спрацьовує,
        # тому offline публікуємо самі.
        try:
            info = self.client.publish(self.topic("status"), "offline",
                                       qos=QOS_STATUS, retain=True)
            info.wait_for_publish(2)
        except Exception:
            pass
        self.client.disconnect()
        self.client.loop_stop()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log(f"[{self.key}] відмова підключення: {reason_code}")
            return
        self.connected = True
        client.publish(self.topic("status"), "online", qos=QOS_STATUS, retain=True)
        if self.is_actuator:
            client.subscribe(self.topic("set"), qos=QOS_COMMAND)
            self.publish_state(force=True)
        log(f"[{self.key}] підключено як {self.user}")

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False
        log(f"[{self.key}] відключено: {reason_code}")

    def _on_message(self, client, userdata, msg):
        pass

    # --- публікація --------------------------------------------------------
    def publish_telemetry(self, value, unit: str) -> None:
        payload = json.dumps({"value": value, "unit": unit, "ts": int(time.time())})
        self.client.publish(self.topic("telemetry"), payload, qos=QOS_TELEMETRY, retain=False)

    def tick(self, now: float) -> None:
        pass

    def publish_state(self, force: bool = False) -> None:
        pass


# ----------------------------------------------------------------------------
# Сенсори
# ----------------------------------------------------------------------------
class PeriodicSensor(Device):
    interval = 5.0
    unit = ""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._next = 0.0

    def read(self):
        raise NotImplementedError

    def tick(self, now: float) -> None:
        if now >= self._next:
            self._next = now + self.interval
            self.publish_telemetry(self.read(), self.unit)


class TemperatureSensor(PeriodicSensor):
    interval, unit = 5.0, "C"

    def read(self):
        with self.world.lock:
            return round(self.world.temp + random.gauss(0, 0.05), 1)


class HumiditySensor(PeriodicSensor):
    interval, unit = 10.0, "%"

    def read(self):
        with self.world.lock:
            return round(self.world.hum + random.gauss(0, 0.2), 1)


class LuxSensor(PeriodicSensor):
    interval, unit = 5.0, "lx"

    def read(self):
        with self.world.lock:
            lux = 2.0 + self.world.daylight_indoor_lux() + self.world.light_lux()
            return int(max(0, lux * random.uniform(0.97, 1.03)))


class PowerMeter(PeriodicSensor):
    interval, unit = 2.0, "W"

    def read(self):
        with self.world.lock:
            return int(round(self.world.power_w()))


class MotionSensor(Device):
    """PIR: спрацьовує випадково, поки в кімнаті є людина, і тримає 1 ще HOLD секунд."""
    HOLD = 8.0
    HEARTBEAT = 5.0

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._last_trigger = 0.0
        self._last_value = None
        self._last_pub = 0.0

    def tick(self, now: float) -> None:
        with self.world.lock:
            present = self.world.presence[self.zone_key]
        if present and random.random() < 0.5:
            self._last_trigger = now
        value = 1 if (now - self._last_trigger) < self.HOLD else 0
        # публікуємо при зміні та як heartbeat
        if value != self._last_value or now - self._last_pub >= self.HEARTBEAT:
            self._last_value, self._last_pub = value, now
            self.publish_telemetry(value, "bool")

    @property
    def zone_key(self) -> str:
        return "living" if self.zone == "livingroom" else "bedroom"


# ----------------------------------------------------------------------------
# Актуатори: підписані на .../set, підтверджують стан у .../state (retain)
# ----------------------------------------------------------------------------
class Actuator(Device):
    is_actuator = True
    HEARTBEAT = 30.0

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._last_sig = None
        self._last_pub = 0.0

    def signature(self):
        raise NotImplementedError

    def state_payload(self) -> dict:
        raise NotImplementedError

    def apply(self, cmd: dict) -> None:
        raise NotImplementedError

    def _on_message(self, client, userdata, msg):
        try:
            cmd = json.loads(msg.payload.decode("utf-8"))
            if not isinstance(cmd, dict):
                raise ValueError("очікувався JSON-об'єкт")
            self.apply(cmd)
            log(f"[{self.key}] команда {cmd}")
        except Exception as exc:                          # некоректна команда ігнорується
            log(f"[{self.key}] ІГНОРУЮ некоректну команду {msg.payload!r}: {exc}")
            return
        self.publish_state(force=True)

    def publish_state(self, force: bool = False) -> None:
        sig = self.signature()
        now = time.time()
        if force or sig != self._last_sig or now - self._last_pub >= self.HEARTBEAT:
            self._last_sig, self._last_pub = sig, now
            payload = dict(self.state_payload(), ts=int(now))
            self.client.publish(self.topic("state"), json.dumps(payload),
                                qos=QOS_STATE, retain=True)

    def tick(self, now: float) -> None:
        self.publish_state()


class Light(Actuator):
    """Диммер 0–100 %. Команди: {"brightness": 70} або {"state": "ON"|"OFF"}."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.last_nonzero = 100

    @property
    def room(self) -> str:
        return "living" if self.zone == "livingroom" else "bedroom"

    def signature(self):
        return self.world.lights[self.room]

    def state_payload(self) -> dict:
        b = self.world.lights[self.room]
        return {"state": "ON" if b > 0 else "OFF", "brightness": b}

    def apply(self, cmd: dict) -> None:
        with self.world.lock:
            if "brightness" in cmd:
                b = int(clamp(round(float(cmd["brightness"])), 0, 100))
            elif "state" in cmd:
                s = str(cmd["state"]).upper()
                if s not in ("ON", "OFF"):
                    raise ValueError("state має бути ON або OFF")
                b = self.last_nonzero if s == "ON" else 0
            else:
                raise ValueError("немає ключів brightness/state")
            if b > 0:
                self.last_nonzero = b
            self.world.lights[self.room] = b


class Heater(Actuator):
    """Обігрівач. Команди: {"state": "ON"|"OFF"} та/або {"setpoint": 21}."""

    def signature(self):
        h = self.world.heater
        return (h["on"], h["setpoint"], h["heating"])

    def state_payload(self) -> dict:
        h = self.world.heater
        return {"state": "ON" if h["on"] else "OFF",
                "setpoint": h["setpoint"], "heating": h["heating"]}

    def apply(self, cmd: dict) -> None:
        if "state" not in cmd and "setpoint" not in cmd:
            raise ValueError("немає ключів state/setpoint")
        with self.world.lock:
            if "state" in cmd:
                s = str(cmd["state"]).upper()
                if s not in ("ON", "OFF"):
                    raise ValueError("state має бути ON або OFF")
                self.world.heater["on"] = (s == "ON")
            if "setpoint" in cmd:
                self.world.heater["setpoint"] = float(clamp(float(cmd["setpoint"]), 5.0, 30.0))


# ----------------------------------------------------------------------------
# Керування сценаріями
# ----------------------------------------------------------------------------
class Simulator:
    def __init__(self) -> None:
        self.world = World()
        w = self.world
        self.devices: list[Device] = [
            MotionSensor("motion_living", "livingroom", "motion", w),
            MotionSensor("motion_bedroom", "bedroom", "motion", w),
            TemperatureSensor("temperature", "livingroom", "temperature", w),
            HumiditySensor("humidity", "livingroom", "humidity", w),
            LuxSensor("lux", "hallway", "lux", w),
            PowerMeter("power", "hallway", "power", w),
            Light("light_living", "livingroom", "light", w),
            Light("light_bedroom", "bedroom", "light", w),
            Heater("heater", "livingroom", "heater", w),
        ]
        self.stop_event = threading.Event()
        self._demo_gen = 0

    # --- сценарії ----------------------------------------------------------
    def trigger(self, name: str) -> None:
        name = name.strip()
        aliases = {"0": "normal", "1": "dark_motion", "2": "away",
                   "3": "return", "4": "overload", "d": "demo"}
        name = aliases.get(name, name)
        if name not in SCENARIOS:
            log(f"[сценарій] невідомий: {name!r}; доступні: {', '.join(SCENARIOS)}")
            return
        self._demo_gen += 1                                # будь-який вибір скасовує demo
        if name == "demo":
            threading.Thread(target=self._demo, args=(self._demo_gen,), daemon=True).start()
        else:
            self.world.set_scenario(name)

    def _demo(self, gen: int) -> None:
        """Повна демонстрація трьох правил. Розрахована на демо-режим openHAB
        (таймери 30 с / 60 с / 15 с). Тривалість ≈ 3,5 хв."""
        timeline = [(0, "normal"), (1, "dark_motion"), (45, "away"),
                    (135, "return"), (165, "overload")]
        log("[demo] старт: темно+рух → пішли з дому → повернулись → перевантаження")
        t0 = time.time()
        for at, name in timeline:
            while time.time() - t0 < at:
                if self._demo_gen != gen or self.stop_event.is_set():
                    return
                time.sleep(0.5)
            if self._demo_gen != gen:
                return
            self.world.set_scenario(name)
        log("[demo] завершено (стан «overload» утримується; команда `normal` скидає)")

    def _fifo_listener(self) -> None:
        if not os.path.exists(CTL_FIFO):
            os.mkfifo(CTL_FIFO, 0o600)
        fd = os.open(CTL_FIFO, os.O_RDWR)                  # RDWR: не блокується й не отримує EOF
        buf = b""
        while not self.stop_event.is_set():
            buf += os.read(fd, 256)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    self.trigger(line.decode())

    def _keyboard_listener(self) -> None:
        log("Клавіші: 1=dark_motion 2=away 3=return 4=overload 0=normal d=demo")
        for line in sys.stdin:
            if line.strip():
                self.trigger(line)

    # --- головний цикл -----------------------------------------------------
    def run(self, scenario: str) -> None:
        missing = [d.key for d in self.devices if not d.password]
        if missing:
            log(f"УВАГА: немає паролів для {missing} (змінні MQTT_PW_*)")
        threading.Thread(target=self._fifo_listener, daemon=True).start()
        if sys.stdin.isatty():
            threading.Thread(target=self._keyboard_listener, daemon=True).start()
        for d in self.devices:
            d.start()
        self.trigger(scenario)
        log(f"Симулятор запущено: {len(self.devices)} пристроїв → {MQTT_HOST}:{MQTT_PORT} (TLS)")

        next_t = time.time()
        while not self.stop_event.is_set():
            self.world.step(1.0)
            now = time.time()
            for d in self.devices:
                d.tick(now)
            next_t += 1.0
            self.stop_event.wait(max(0.0, next_t - time.time()))

        log("Зупинка: публікую offline та відключаюсь")
        for d in self.devices:
            d.stop()


def send_trigger(name: str) -> int:
    if not os.path.exists(CTL_FIFO):
        print(f"Симулятор не запущений (немає {CTL_FIFO})", file=sys.stderr)
        return 1
    fd = os.open(CTL_FIFO, os.O_WRONLY | os.O_NONBLOCK)
    os.write(fd, (name + "\n").encode())
    os.close(fd)
    print(f"Надіслано сценарій: {name}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Симулятор IoT-пристроїв розумної квартири")
    ap.add_argument("--scenario", choices=SCENARIOS, default="normal",
                    help="початковий сценарій (за замовчуванням normal)")
    ap.add_argument("--trigger", choices=SCENARIOS,
                    help="надіслати сценарій уже запущеному симулятору й вийти")
    args = ap.parse_args()

    if args.trigger:
        return send_trigger(args.trigger)

    sim = Simulator()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: sim.stop_event.set())
    sim.run(args.scenario)
    return 0


if __name__ == "__main__":
    sys.exit(main())
