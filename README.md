# LYWSD02 温湿度计 · 自建 Web 看板

把米家 LYWSD02（LYWSD02MMC 同族）电子温湿度计接进自己的电脑：定时读温湿度、每天自动校时、历史存 SQLite、网页看曲线。

不需要小米账号、不需要 bindkey、不需要刷固件。

## 功能

- **定时采集**：BLE 连接 `EBE0CCC1` 订阅 3 字节读数，连 2~3 秒就断开，默认每 10 分钟一次
- **自动校时**：向 `EBE0CCB7` 写 5 字节（UTC epoch + 时区），每天 02:00 校一次
- **时钟漂移**：读设备当前时间与本机比对，网页上能看到它快/慢了多少秒
- **历史曲线**：24h / 7d / 30d，数据落 SQLite（只追加，不覆盖）
- **断线可见**：连不上时网页直接显示错误原因，不会假装有数据

## 快速开始

```bash
# macOS / Linux
./run.command

# Windows
run.bat
```

然后打开 <http://127.0.0.1:8199/>

首次启动会创建 `.venv` 并安装 `aiohttp` + `bleak`。设备 MAC 留空时会按名字前缀 `LYWSD02` 扫描发现；想固定设备就在网页「设置」里填 MAC。

## 配置文件

`data/thermo.json`（首次运行自动生成）：

| 字段 | 说明 | 默认 |
|---|---|---|
| `enabled` | 是否启用定时采集 | `true` |
| `name` | BLE 广播名称前缀 | `LYWSD02` |
| `mac` | 设备地址（留空则自动扫描） | `""` |
| `poll_interval` | 采集间隔（秒，60~21600） | `600` |
| `sync_at` | 每天校时时间 `HH:MM` | `02:00` |

## Windows 说明

`bleak` 在 Windows 上走 WinRT，需要：

- Windows 10 1803+ / Windows 11
- 蓝牙 4.0+ 适配器，且系统里已配对过该温湿度计
- Python 3.10+

与 macOS 的差异：Windows 上 BLE 扫描通常更快，但**第一次连接需要在系统设置里完成配对**，否则会报 `Access is denied`。

## 数据

- `data/thermo.db`：SQLite，三张表（读数、元信息），只追加
- 上报的温度是 `int16(温度 × 100)`，湿度是 `uint8`，单位为 %RH

## 协议备忘

| 用途 | UUID | 数据 |
|---|---|---|
| 读温湿度（notify） | `ebe0ccc1-…` | `int16 LE` 温度×100 + `uint8` 湿度 |
| 校时（write） | `ebe0ccb7-…` | `uint32 LE` UTC epoch + `int8` 时区小时 |

**校时必须传 UTC**：设备把写入值当作 UTC 存，自己按内部时区显示。传本地 epoch 会让屏幕时间多出时区偏移。

## 许可

MIT
