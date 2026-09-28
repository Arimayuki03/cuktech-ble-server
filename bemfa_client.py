"""Bemfa cloud MQTT client + HTTP API.

Fully aligned with the Bemfa HA integration's Python logic:
- Topic format: "hass" + md5(entity_id) + "006" (switch)
- MQTT: subscribe "{topic}" for commands, publish "{topic}/set" for state
- HTTP: POST to api.bemfa.com for topic registration
- Keepalive: ping/pong every 30s (hassping topic), reconnect after 3 lost
"""
import asyncio
import hashlib
import logging
import threading
import time
from typing import Callable, Optional

import aiohttp
import paho.mqtt.client as mqtt

_LOGGER = logging.getLogger(__name__)

# Bemfa constants (aligned with HA integration)
MQTT_HOST = "bemfa.com"
MQTT_PORT = 9501
MQTT_KEEPALIVE = 600

TOPIC_PREFIX = "hass"
TOPIC_PUBLISH = "{topic}/set"  # publish state here
TOPIC_PING = f"{TOPIC_PREFIX}ping"

INTERVAL_PING_SEND = 30      # send ping every 30s
INTERVAL_PING_RECEIVE = 20   # detect lost after 20s
MAX_PING_LOST = 3            # reconnect after 3 consecutive lost

CREATE_TOPIC_URL = "http://api.bemfa.com/api/user/addtopic/"
DELETE_TOPIC_URL = "http://api.bemfa.com/api/user/deltopic/"

MSG_ON = "on"
MSG_OFF = "off"


class BemfaDevice:
    """Represents a Bemfa switch device."""

    def __init__(self, entity_id: str, name: str):
        self.entity_id = entity_id
        self.name = name
        self._topic: Optional[str] = None
        self._pub_topic: Optional[str] = None

    @property
    def topic(self) -> str:
        if self._topic is None:
            md5 = hashlib.md5(self.entity_id.encode("utf-8")).hexdigest()
            self._topic = f"{TOPIC_PREFIX}{md5}006"
        return self._topic

    @property
    def pub_topic(self) -> str:
        if self._pub_topic is None:
            self._pub_topic = TOPIC_PUBLISH.format(topic=self.topic)
        return self._pub_topic


class BemfaClient:
    """Bemfa cloud MQTT client with HTTP topic registration."""

    def __init__(self, uid: str, modified: bool = False):
        self._uid = uid
        self._modified = modified
        self._client: Optional[mqtt.Client] = None
        self._devices: dict[str, BemfaDevice] = {}
        self._state_cache: dict[str, str] = {}  # topic -> "on"/"off"
        self._command_callbacks: dict[str, Callable[[bool], None]] = {}
        self._connected = False
        self._connect_time = 0.0
        self._lock = threading.Lock()
        # Ping/pong keepalive (aligned with HA integration)
        self._ping_lost = 0
        self._ping_publish_task: Optional[asyncio.Task] = None
        self._ping_receive_task: Optional[asyncio.Task] = None
        self._reconnect_count = 0
        # 退避重连状态(见 _connect_mqtt / _schedule_reconnect / stop)
        self._reconnect_attempt = 0  # 退避计数,连接成功后在 _on_connect 归零
        self._stopped = False        # stop() 后禁止一切重连,防止僵尸 Timer
        self._reconnect_timer: Optional[threading.Timer] = None  # 挂起的重连 Timer(单链)
        self._connect_in_flight = False  # _connect_mqtt 并发建连防护

    @property
    def is_connected(self) -> bool:
        return self._connected

    def quality(self) -> dict:
        """Return Bemfa connection quality metrics."""
        if not self._connected:
            return {"score": 0, "uptime": 0, "ping_lost": self._ping_lost,
                    "reconnect_count": self._reconnect_count}
        uptime = int(time.time() - self._connect_time) if self._connect_time else 0
        # Ping loss penalty: each lost ping costs 15 points
        ping_score = max(0, 100 - self._ping_lost * 15)
        # Reconnect penalty: each reconnect costs 10 points
        reconnect_score = max(0, 100 - self._reconnect_count * 10)
        score = round(ping_score * 0.6 + reconnect_score * 0.4)
        return {"score": score, "uptime": uptime, "ping_lost": self._ping_lost,
                "reconnect_count": self._reconnect_count}

    def add_device(self, entity_id: str, name: str) -> BemfaDevice:
        """Register a device to sync with Bemfa."""
        dev = BemfaDevice(entity_id, name)
        self._devices[entity_id] = dev
        return dev

    def on_command(self, entity_id: str, callback: Callable[[bool], None]):
        """Register command callback for a device."""
        self._command_callbacks[entity_id] = callback

    async def start(self):
        """Connect MQTT and register topics."""
        if self._client:
            return

        # Register topics via HTTP
        await self._register_topics()

        # Connect MQTT (in thread pool since paho is synchronous)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._connect_mqtt)

        # Start ping/pong keepalive (aligned with HA integration)
        self._ping_lost = 0
        self._start_ping_cycle()
        if self._client is None:
            # _connect_mqtt 失败路径会置 _client=None 并安排退避重试
            _LOGGER.warning("Bemfa MQTT 首连失败,已安排退避重试")
        else:
            _LOGGER.info("Bemfa client started")

    async def stop(self):
        """Disconnect MQTT and cleanup."""
        # 先置停止标志并取消挂起的重连 Timer,防止停止后僵尸重连。
        # 置位与 _connect_mqtt 锁内复查共用 _lock,保证 happens-before。
        with self._lock:
            self._stopped = True
            timer = self._reconnect_timer
            self._reconnect_timer = None
        if timer:
            timer.cancel()

        # Cancel ping tasks
        for task in (self._ping_publish_task, self._ping_receive_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._ping_publish_task = None
        self._ping_receive_task = None

        if self._client:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._disconnect_mqtt)
            self._client = None

        self._connected = False
        _LOGGER.info("Bemfa client stopped")

    def publish_state(self, entity_id: str, state: str):
        """Publish state to Bemfa. Called from any thread."""
        with self._lock:
            if not self._client or not self._connected:
                return
            dev = self._devices.get(entity_id)
            if not dev:
                return
            self._state_cache[dev.topic] = state
            self._client.publish(dev.pub_topic, state, qos=1, retain=True)

    # ---- MQTT ----

    def _connect_mqtt(self):
        # 并发/停止防护:_stopped 或已有建连在进行中则直接返回,
        # 防止多条重连链并发覆盖 _client(paho 网络线程与 socket 泄漏)。
        if self._stopped or self._connect_in_flight:
            return
        with self._lock:
            if self._stopped or self._connect_in_flight or self._client is not None:
                return
            self._connect_in_flight = True
        try:
            self._client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, self._uid, mqtt.MQTTv311
            )
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.on_message = self._on_message

            try:
                self._client.connect(MQTT_HOST, MQTT_PORT, MQTT_KEEPALIVE)
                self._client.loop_start()
                _LOGGER.info("Bemfa MQTT connecting to %s:%s", MQTT_HOST, MQTT_PORT)
            except Exception as e:
                _LOGGER.error("Bemfa MQTT connection failed: %s", e)
                try:
                    self._client.loop_stop()
                except Exception:
                    pass
                self._client = None
                # 首连失败必须留出重试通道：ping 循环的未连接分支只重排自身,
                # 永远不会再调 _connect_mqtt——不安排重试的话，启动时网络抖动
                # （DNS 未就绪等）会让语音控制静默失效到进程重启。
                # in-flight 先清掉,否则重连 Timer 触发时会被入口守卫挡掉。
                self._connect_in_flight = False
                self._schedule_reconnect()
        finally:
            # 保守清防护位(覆盖 loop_start 成功但 on_connect 尚未回调的窗口);
            # 与调度重连的重叠防护主要靠 _client is not None + 单链 Timer。
            self._connect_in_flight = False

    _RECONNECT_DELAYS = (5, 15, 30, 60)  # 秒，封顶后按 60s 周期重试

    def _schedule_reconnect(self):
        """线程安全地安排一次延迟重连(可从 executor 线程调用)。

        通过先取消旧 Timer 保证任意时刻只有一条重连链,
        `_reconnect_attempt` 的读改写在锁内完成。
        """
        with self._lock:
            if self._stopped:
                return
            # 取消旧的挂起 Timer,保证单条重连链,计数不被交错递增
            if self._reconnect_timer is not None:
                self._reconnect_timer.cancel()
                self._reconnect_timer = None
            delay = self._RECONNECT_DELAYS[min(self._reconnect_attempt,
                                               len(self._RECONNECT_DELAYS) - 1)]
            self._reconnect_attempt += 1
            _LOGGER.warning("Bemfa MQTT retry #%d in %ds", self._reconnect_attempt, delay)
            timer = threading.Timer(delay, self._retry_connect)
            timer.daemon = True
            self._reconnect_timer = timer
        timer.start()

    def _retry_connect(self):
        """Timer 线程内直接同步重试 MQTT 连接(成功后 attempt 归零在 _on_connect)。"""
        self._connect_mqtt()

    def _disconnect_mqtt(self):
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None
        self._connected = False

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            with self._lock:
                self._connected = True
            self._connect_time = time.time()
            self._reconnect_attempt = 0  # 成功连接：重连退避归零
            _LOGGER.info("Bemfa MQTT connected")

            # Subscribe to all device topics
            for dev in self._devices.values():
                client.subscribe(dev.topic, 1)
                _LOGGER.debug("Bemfa subscribed: %s", dev.topic)

            # Subscribe to ping topic
            client.subscribe(TOPIC_PING, 1)

            # Publish initial states so Bemfa marks device online
            with self._lock:
                for dev in self._devices.values():
                    state = self._state_cache.get(dev.topic, MSG_OFF)
                    client.publish(dev.pub_topic, state, qos=1, retain=True)
        else:
            _LOGGER.warning("Bemfa MQTT connect failed: rc=%s", rc)

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        with self._lock:
            self._connected = False
        _LOGGER.warning("Bemfa MQTT disconnected (rc=%s)", rc)

    def _on_message(self, client, userdata, message):
        topic = message.topic
        data = message.payload.decode("utf-8", errors="replace")

        # Handle ping pong (aligned with HA integration)
        if topic == TOPIC_PING:
            if self._ping_receive_task is not None:
                self._ping_receive_task.cancel()
                self._ping_receive_task = None
                self._ping_lost = 0
            return

        # Find device by topic
        for entity_id, dev in self._devices.items():
            if dev.topic == topic:
                # Ignore echo during grace period (10s after connect, aligned with ESP32)
                now = time.time()
                if now - self._connect_time < 10:
                    _LOGGER.debug("Bemfa ignoring echo: %s=%s", topic, data)
                    return

                # ── Echo avoidance: if received state matches cached state, skip ──
                cached = self._state_cache.get(topic)
                incoming = data.strip().lower()
                if cached == incoming:
                    _LOGGER.debug("Bemfa skipping echo (state unchanged): %s=%s", entity_id, data)
                    return

                # ── Command debounce: skip duplicate cmd within 1s ──
                last = getattr(self, '_last_cmd_time', {}).get(entity_id)
                if last and now - last < 1.0:
                    _LOGGER.debug("Bemfa debounce: %s=%s (%.1fs since last)", entity_id, data, now - last)
                    return

                on = incoming == "on"
                _LOGGER.info("Bemfa recv: %s=%s", entity_id, data)

                # Execute command
                cb = self._command_callbacks.get(entity_id)
                if cb is None:
                    _LOGGER.warning("Bemfa no callback registered for %s", entity_id)
                    return
                try:
                    ok = cb(on)
                    if ok:
                        with self._lock:
                            self._state_cache[topic] = MSG_ON if on else MSG_OFF
                        if not hasattr(self, '_last_cmd_time'):
                            self._last_cmd_time = {}
                        self._last_cmd_time[entity_id] = now
                except Exception as e:
                    _LOGGER.error("Bemfa command error: %s=%s: %s", entity_id, data, e)
                break

    # ---- HTTP API ----

    async def _register_topics(self):
        """Register topics with Bemfa. Deletes existing topics first only when names changed."""
        _LOGGER.info("Bemfa registering %d topics... (modified=%s)", len(self._devices), self._modified)
        async with aiohttp.ClientSession() as session:
            for dev in self._devices.values():
                if self._modified:
                    # Names changed — delete old topic before re-creating
                    try:
                        del_resp = await session.post(
                            DELETE_TOPIC_URL,
                            data={"uid": self._uid, "topic": dev.topic, "type": 1},
                            timeout=aiohttp.ClientTimeout(total=5),
                        )
                        _LOGGER.info("Bemfa delete %s: HTTP %d %s", dev.topic, del_resp.status, await del_resp.text())
                    except Exception as e:
                        _LOGGER.warning("Bemfa delete error: %s: %s", dev.topic, e)
                    await asyncio.sleep(0.1)

                try:
                    add_resp = await session.post(
                        CREATE_TOPIC_URL,
                        data={
                            "uid": self._uid,
                            "topic": dev.topic,
                            "type": 1,
                            "name": dev.name,
                        },
                        timeout=aiohttp.ClientTimeout(total=5),
                    )
                    body = await add_resp.text()
                    _LOGGER.info("Bemfa add %s (%s): HTTP %d %s", dev.topic, dev.name, add_resp.status, body)
                except Exception as e:
                    _LOGGER.warning("Bemfa add error: %s: %s", dev.topic, e)
                await asyncio.sleep(0.1)

    # ---- Ping/Pong Keepalive (aligned with HA integration) ----

    def _start_ping_cycle(self):
        """Start a ping cycle. Recursive: each cycle schedules the next.
        
        Matches official HA integration's _ping() pattern exactly.
        Only ONE receive task exists at any time.
        """
        async def _publish_job():
            await asyncio.sleep(INTERVAL_PING_SEND)
            with self._lock:
                if not self._client or not self._connected:
                    # Not connected yet — retry next cycle
                    self._start_ping_cycle()
                    return
                self._client.publish(TOPIC_PING, "ping")  # QoS 0 (matches official)
            # Start receive monitor for THIS cycle
            self._ping_receive_task = asyncio.create_task(_receive_job())
            # Schedule next cycle (recursive, ensures one-at-a-time)
            self._start_ping_cycle()

        async def _receive_job():
            await asyncio.sleep(INTERVAL_PING_RECEIVE)
            self._ping_lost += 1
            _LOGGER.warning("Bemfa ping lost (%d/%d)", self._ping_lost, MAX_PING_LOST)
            if self._ping_lost == MAX_PING_LOST:  # == (matches official)
                self._ping_lost = 0
                _LOGGER.warning("Bemfa max ping lost, reconnecting...")
                await self._reconnect()

        self._ping_publish_task = asyncio.create_task(_publish_job())

    async def _reconnect(self):
        """Disconnect and reconnect MQTT, restart ping cycle."""
        self._ping_lost = 0
        self._reconnect_count += 1
        # Cancel publish chain only (future cycles), not _ping_receive_task
        if self._ping_publish_task and not self._ping_publish_task.done():
            self._ping_publish_task.cancel()
            try:
                await self._ping_publish_task
            except asyncio.CancelledError:
                pass
        self._ping_publish_task = None
        self._ping_receive_task = None
        # Disconnect MQTT
        loop = asyncio.get_running_loop()
        if self._client:
            await loop.run_in_executor(None, self._disconnect_mqtt)
        # Reconnect MQTT
        await loop.run_in_executor(None, self._connect_mqtt)
        # Restart ping cycle (fresh chain, no duplicate tasks)
        self._start_ping_cycle()
