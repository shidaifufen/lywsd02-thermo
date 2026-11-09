"""跨模块的 BLE 互斥锁。

温湿度计和土豆片是两套独立功能，但共用同一块蓝牙：传图时不该再插一次
温湿度连接，反之亦然。锁放在中立模块里，两边都 import 它，谁也不依赖谁。
"""
import asyncio

BLE_GUARD = asyncio.Lock()
