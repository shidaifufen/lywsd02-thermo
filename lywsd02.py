"""米家电子温湿度计（LYWSD02 / LYWSD02MMC 同族）接入。

自建的最小实现，只做三件事：

1. **定时读温湿度**：BLE 连上去订阅 ``EBE0CCC1``，设备每秒推一条 3 字节
   ``int16(温度×100, 小端) + uint8(湿度%)``。读几条取中位数后立刻断开，
   一次连接 2~3 秒。
2. **每天校时**：向 ``EBE0CCB7`` 写 5 字节 ``uint32(本地墙钟 epoch, 小端) + int8(时区小时)``。
   设备读到的就是"墙上时间"，所以要传 ``time.time() + utc_offset`` 而不是 UTC。
3. **增量落库**：SQLite 只追加，不更新历史行；网页按时间窗口取。

为什么不走 MiBeacon 被动广播：那需要 bindkey（小米云扫码再登一次），而连接读取
既不需要凭据、又能顺带读回设备当前时间算漂移。10 分钟一次的连接对电池的影响
可以忽略（米家 App 自己也是这么连的）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import struct
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import web

try:  # 只有真正用到 BLE 时才需要 bleak；模块导入本身不绑定平台
    from bleak import BleakClient, BleakScanner
except Exception:  # pragma: no cover - 没有 bleak 时给出明确错误而不是崩在 import
    BleakClient = None
    BleakScanner = None

_LOGGER = logging.getLogger("cuktech_thermo")

# 设备特征（小米私有 128 位 UUID）
UUID_TIME = "ebe0ccb7-7a0a-4b0c-8a1a-6ff2997da3a6"
UUID_NOTIFY = "ebe0ccc1-7a0a-4b0c-8a1a-6ff2997da3a6"

DEFAULT_NAME = "LYWSD02"
DEFAULT_POLL_SEC = 600
DEFAULT_SYNC_AT = "02:00"
CONNECT_TIMEOUT = 25.0
NOTIFY_WAIT = 12.0

# 全局 BLE 互斥：与土豆片共用同一块蓝牙，锁放在中立模块里（两边互不 import）
from ble_lock import BLE_GUARD


def _utc_offset_seconds() -> float:
    return datetime.now().astimezone().utcoffset().total_seconds()


def _device_epoch() -> int:
    """设备存的是 **UTC epoch**，显示时用自己的时区字节换算成本地时间。

    实测：写"本地墙钟 epoch"（+8h）进去，显示屏会再 +8h，变成 05:08；
    读回来的值也正好等于 UTC epoch。所以这里写纯 UTC，时区交给第 5 个字节。
    """
    return int(time.time())


class ThermoDB:
    """只追加的温湿度采样库。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS samples ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " ts INTEGER NOT NULL, temp REAL NOT NULL, hum INTEGER NOT NULL,"
            " source TEXT NOT NULL DEFAULT 'poll')"
        )
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts)")
        self._conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        self._conn.commit()

    # ── 写入 ──
    def add(self, ts: int, temp: float, hum: int, source: str = "poll") -> bool:
        with self._lock:
            last = self._conn.execute(
                "SELECT ts, temp, hum FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
            if last and ts - last[0] < 60 and abs(last[1] - temp) < 0.011 and last[2] == hum:
                return False  # 一分钟内完全相同的点不落库，重启时不会写出重复行
            self._conn.execute(
                "INSERT INTO samples (ts, temp, hum, source) VALUES (?,?,?,?)",
                (ts, round(temp, 2), int(hum), source))
            self._conn.commit()
            return True

    def last(self):
        with self._lock:
            row = self._conn.execute(
                "SELECT ts, temp, hum FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
        return {"ts": row[0], "temp": row[1], "hum": row[2]} if row else None

    def series(self, hours: int = 24):
        since = int(time.time()) - hours * 3600
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, temp, hum FROM samples WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
        return [{"ts": r[0], "temp": r[1], "hum": r[2]} for r in rows]

    def stats(self, hours: int = 24):
        since = int(time.time()) - hours * 3600
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*), MIN(temp), MAX(temp), AVG(temp), MIN(hum), MAX(hum), AVG(hum)"
                " FROM samples WHERE ts >= ?", (since,)).fetchone()
        if not row or not row[0]:
            return None
        return {
            "count": row[0],
            "temp_min": None if row[1] is None else round(row[1], 2),
            "temp_max": None if row[2] is None else round(row[2], 2),
            "temp_avg": None if row[3] is None else round(row[3], 2),
            "hum_min": row[4], "hum_max": row[5],
            "hum_avg": None if row[6] is None else round(row[6], 1),
        }

    # ── meta ──
    def get_meta(self, key: str, default=None):
        with self._lock:
            row = self._conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value):
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, str(value)))
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()


class ThermoService:
    """温湿度计的采集 / 校时 / 调度。

    调度是"每秒看一眼该不该干"，而不是 sleep(600)：
    Mac 睡眠唤醒后也能立刻补上一次采集，且不会因为改配置要重启任务。
    """

    def __init__(self, data_dir: Path, sse=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cfg_path = self.data_dir / "thermo.json"
        self.db = ThermoDB(self.data_dir / "thermo.db")
        self.sse = sse
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._cfg = self._load_cfg()
        self._state = {
            "available": False,          # 上一次采集是否成功
            "error": "",
            "temp": None, "hum": None,
            "sample_ts": None,
            "drift_sec": None,           # 设备时钟 - 本机墙钟
            "device_clock": None,
            "last_sync_ts": int(self.db.get_meta("last_sync_ts", "0") or 0),
            "last_drift_sec": (int(self.db.get_meta("drift_sec")) if self.db.get_meta("drift_sec") else None),
            "last_sync_ok": self.db.get_meta("last_sync_ok", "0") == "1",
            "last_sync_error": self.db.get_meta("last_sync_error", "") or "",
            "busy": False,
        }
        last = self.db.last()
        if last:
            self._state.update(temp=last["temp"], hum=last["hum"],
                               sample_ts=last["ts"], available=True)

    # ── 配置 ──
    def _load_cfg(self) -> dict:
        cfg = {
            "enabled": True,
            "name": DEFAULT_NAME,
            "mac": "",
            "poll_interval": DEFAULT_POLL_SEC,
            "sync_at": DEFAULT_SYNC_AT,
        }
        try:
            if self.cfg_path.exists():
                cfg.update(json.loads(self.cfg_path.read_text("utf-8")) or {})
        except Exception as e:
            _LOGGER.warning("thermo.json 读取失败，用默认配置: %s", e)
        return cfg

    def save_cfg(self, patch: dict) -> dict:
        cur = dict(self._cfg)
        if "enabled" in patch:
            cur["enabled"] = bool(patch["enabled"])
        if "name" in patch:
            cur["name"] = str(patch["name"]).strip() or DEFAULT_NAME
        if "mac" in patch:
            cur["mac"] = str(patch["mac"]).strip()
        if "poll_interval" in patch:
            try:
                cur["poll_interval"] = max(60, min(3600 * 6, int(patch["poll_interval"])))
            except (TypeError, ValueError):
                pass
        if "sync_at" in patch:
            v = str(patch["sync_at"]).strip()
            try:
                hh, mm = v.split(":")
                assert 0 <= int(hh) < 24 and 0 <= int(mm) < 60
                cur["sync_at"] = f"{int(hh):02d}:{int(mm):02d}"
            except Exception:
                pass
        self._cfg = cur
        self.cfg_path.write_text(json.dumps(cur, ensure_ascii=False, indent=2), "utf-8")
        return cur

    @property
    def cfg(self) -> dict:
        return dict(self._cfg)

    # ── BLE 基础操作 ──
    async def _find_device(self):
        if BleakScanner is None:
            raise RuntimeError("缺少 bleak 依赖")
        name = self._cfg.get("name") or DEFAULT_NAME
        mac = (self._cfg.get("mac") or "").strip()

        def match(d, adv):
            if mac and mac.lower() in (d.address or "").lower():
                return True
            return (d.name or adv.local_name or "") == name

        # 扫两轮：旁边如果有别的 BLE 任务在扫描，一轮 15 秒偶尔会空手而归
        for attempt in range(2):
            dev = await BleakScanner.find_device_by_filter(match, timeout=15)
            if dev is not None:
                return dev
            if attempt == 0:
                await asyncio.sleep(2)
        raise RuntimeError(f"没扫到「{name}」（扫了 2 轮各 15 秒），确认它在附近且电池有电")

    async def sample(self, source: str = "poll") -> dict:
        """连上去读一次温湿度（顺带读设备时钟算漂移），然后立刻断开。"""
        if self._state["busy"]:
            return {"ok": False, "error": "上一次采集还没结束"}
        self._state["busy"] = True
        try:
            async with BLE_GUARD:
                readings: list[bytes] = []

                def _cb(_ch, data):
                    readings.append(bytes(data))

                dev = await self._find_device()
                async with BleakClient(dev, timeout=CONNECT_TIMEOUT) as client:
                    await client.start_notify(UUID_NOTIFY, _cb)
                    deadline = time.monotonic() + NOTIFY_WAIT
                    while len(readings) < 2 and time.monotonic() < deadline:
                        await asyncio.sleep(0.2)
                    try:
                        clock_raw = await client.read_gatt_char(UUID_TIME)
                    except Exception:
                        clock_raw = b""
                    try:
                        await client.stop_notify(UUID_NOTIFY)
                    except Exception:
                        pass

            if not readings:
                raise RuntimeError("连上了但没收到温湿度通知（设备可能正被米家 App 占用）")
            vals = []
            for raw in readings[:4]:
                if len(raw) >= 3:
                    vals.append((struct.unpack_from("<h", raw, 0)[0] / 100.0, raw[2]))
            if not vals:
                raise RuntimeError("温湿度数据格式不认识")
            vals.sort()
            temp, hum = vals[len(vals) // 2]

            drift = None
            dev_epoch = None
            if len(clock_raw) >= 5:
                dev_epoch = int.from_bytes(clock_raw[:4], "little")
                drift = dev_epoch - int(time.time())

            ts = int(time.time())
            self.db.add(ts, temp, hum, source=source)
            if drift is not None:
                self.db.set_meta("drift_sec", drift)
            self._state.update(available=True, error="", temp=round(temp, 2), hum=int(hum),
                               sample_ts=ts, drift_sec=drift, device_clock=dev_epoch)
            db_last = self.db.last()
            if db_last:
                self._state["sample_ts"] = db_last["ts"]
            self._emit()
            _LOGGER.info("温湿度 %.2f°C %d%%（%s）", temp, hum, source)
            return {"ok": True, "temp": round(temp, 2), "hum": int(hum), "ts": ts,
                    "drift_sec": drift}
        except Exception as e:
            self._state.update(available=False, error=str(e))
            self._emit()
            _LOGGER.warning("温湿度采集失败: %s", e)
            return {"ok": False, "error": str(e)}
        finally:
            self._state["busy"] = False

    async def sync_time(self) -> dict:
        """把设备时间校准到本机时间。"""
        if self._state["busy"]:
            return {"ok": False, "error": "上一次操作还没结束"}
        self._state["busy"] = True
        try:
            offset = _utc_offset_seconds()
            payload = struct.pack("<I", _device_epoch()) + bytes(
                [int(round(offset / 3600)) & 0xFF])
            async with BLE_GUARD:
                dev = await self._find_device()
                async with BleakClient(dev, timeout=CONNECT_TIMEOUT) as client:
                    await client.write_gatt_char(UUID_TIME, payload, response=True)
                    await asyncio.sleep(0.4)
                    back = await client.read_gatt_char(UUID_TIME)
            dev_epoch = int.from_bytes(back[:4], "little") if len(back) >= 4 else None
            drift = None if dev_epoch is None else dev_epoch - int(time.time())
            now = int(time.time())
            self.db.set_meta("last_sync_ts", now)
            self.db.set_meta("last_sync_ok", "1")
            self.db.set_meta("last_sync_error", "")
            self._state.update(last_sync_ts=now, last_sync_ok=True, last_sync_error="",
                               drift_sec=drift, device_clock=dev_epoch)
            self._emit()
            _LOGGER.info("设备校时完成，写回 %s，漂移 %ss", dev_epoch, drift)
            return {"ok": True, "device_clock": dev_epoch, "drift_sec": drift, "ts": now}
        except Exception as e:
            now = int(time.time())
            self.db.set_meta("last_sync_ts", now)
            self.db.set_meta("last_sync_ok", "0")
            self.db.set_meta("last_sync_error", str(e))
            self._state.update(last_sync_ts=now, last_sync_ok=False, last_sync_error=str(e))
            self._emit()
            _LOGGER.warning("设备校时失败: %s", e)
            return {"ok": False, "error": str(e)}
        finally:
            self._state["busy"] = False

    # ── 调度 ──
    def _next_sync_dt(self, now: datetime) -> datetime:
        hh, mm = (int(x) for x in str(self._cfg.get("sync_at", DEFAULT_SYNC_AT)).split(":"))
        return now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    async def _loop(self):
        _LOGGER.info("温湿度调度器启动（间隔 %ss，校时 %s）",
                     self._cfg.get("poll_interval"), self._cfg.get("sync_at"))
        while not self._stop.is_set():
            try:
                if self._cfg.get("enabled"):
                    now = datetime.now()
                    last_sample = self._state.get("sample_ts") or 0
                    if time.time() - last_sample >= int(self._cfg.get("poll_interval", 600)):
                        await self.sample("poll")
                    # 校时：到点且今天还没校过 → 立刻补上（睡眠唤醒后也成立）
                    due = self._next_sync_dt(now)
                    last_sync = int(self._state.get("last_sync_ts") or 0)
                    if now >= due and last_sync < int(due.timestamp()):
                        await self.sync_time()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _LOGGER.warning("温湿度调度异常: %s", e)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=20)
            except asyncio.TimeoutError:
                pass

    def start(self):
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="thermo-scheduler")

    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.db.close()

    def _emit(self):
        if not self.sse:
            return
        try:
            self.sse.emit("thermo", {"thermo": self.status()})
        except Exception:
            pass

    # ── 对外状态 ──
    def status(self) -> dict:
        s = dict(self._state)
        s["enabled"] = bool(self._cfg.get("enabled"))
        s["poll_interval"] = self._cfg.get("poll_interval")
        s["sync_at"] = self._cfg.get("sync_at")
        s["name"] = self._cfg.get("name")
        s["mac"] = self._cfg.get("mac")
        now = int(time.time())
        s["age_sec"] = None if not s.get("sample_ts") else now - s["sample_ts"]
        due = self._next_sync_dt(datetime.now())
        # 今天这一次已经校过了 → 下一次是明天同一时刻，不要显示一个已经过去的时间
        if int(s.get("last_sync_ts") or 0) >= int(due.timestamp()):
            due = due + timedelta(days=1)
        s["sync_due_ts"] = int(due.timestamp())
        if s.get("drift_sec") is None:
            s["drift_sec"] = s.get("last_drift_sec")
        return s


# ─────────────────────────────────────────────────────────────────────────────
# 单例与 HTTP 路由
# ─────────────────────────────────────────────────────────────────────────────
_SVC: ThermoService | None = None


def init_service(data_dir, sse=None) -> ThermoService:
    global _SVC
    _SVC = ThermoService(Path(data_dir), sse=sse)
    return _SVC


def current() -> ThermoService:
    if _SVC is None:
        raise RuntimeError("thermo service 未初始化")
    return _SVC


def make_routes():
    async def _status(request):
        svc = current()
        return web.json_response({"ok": True, "thermo": svc.status(),
                                  "stats24h": svc.db.stats(24),
                                  "stats7d": svc.db.stats(24 * 7)})

    async def _series(request):
        svc = current()
        try:
            hours = max(1, min(24 * 30, int(request.query.get("hours", 24))))
        except ValueError:
            hours = 24
        return web.json_response({"ok": True, "hours": hours, "points": svc.db.series(hours)})

    async def _sample(request):
        res = await current().sample(source="manual")
        return web.json_response(res, status=200 if res.get("ok") else 500)

    async def _sync(request):
        res = await current().sync_time()
        return web.json_response(res, status=200 if res.get("ok") else 500)

    async def _config(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        svc = current()
        cfg = svc.save_cfg(body or {})
        return web.json_response({"ok": True, "config": cfg, "thermo": svc.status()})

    return [
        ("GET", "/api/thermo", _status),
        ("GET", "/api/thermo/series", _series),
        ("POST", "/api/thermo/sample", _sample),
        ("POST", "/api/thermo/sync", _sync),
        ("POST", "/api/thermo/config", _config),
    ]
