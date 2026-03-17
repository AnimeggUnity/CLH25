# 台科電 DEM540N 本地設定指南

> 整理日期：2026-03-17
> 測試環境：COM3, 2400 bps, 8N1, Modbus RTU

---

## 一、通訊參數

| 項目 | 值 |
|------|----|
| 介面 | RS485 轉 USB（FTDI, VID:PID=0403:6001） |
| COM Port | COM3 |
| 鮑率 | 2400 bps |
| 資料格式 | 8N1（8 bits, None, 1 stop） |
| 協議 | Modbus RTU |
| CRC | CRC-16 Modbus（Little Endian） |

---

## 二、已確認可用的功能指令

### FC03 — 讀取暫存器（基本讀取）
```
TX: [ID] 03 00 00 00 18 [CRC]
RX: [ID] 03 30 [48 bytes data] [CRC]
```
- 位址 0x0000，長度 0x0018（24 個暫存器 = 48 bytes）

### FC05 — 控制指令（單一線圈寫入）
```
TX: [ID] 05 [Addr H] [Addr L] [Val H] [Val L] [CRC]
```

| 功能 | 位址 | 值 |
|------|------|----|
| 開啟供電 | 0x0002 | 0xFF00 |
| 強制關閉 | 0x0002 | 0x0000 |
| 進入預付模式 | 0x000B | 0xFF00 |
| 切回普通模式 | 0x000B | 0x0000 |
| 預付模式暫時斷電 | 0x0052 | 0x0000 |
| 預付模式恢復供電 | 0x0052 | 0xFF00 |

### FC10 — 寫入多個暫存器（重要！）
```
TX: [ID] 10 [Addr H] [Addr L] [Count H] [Count L] [ByteCount] [Data...] [CRC]
```

| 功能 | 位址 | 說明 |
|------|------|------|
| **修改裝置 ID** | 0x0005 | 寫入新 ID（1~247），立即生效，不需斷電 |
| 儲值（剩餘度數） | 0x0016 | 32-bit，單位 ×100（例：5000 = 50.00 度） |

> ⚠️ FC06（單一暫存器寫入）對 reg 0x0005 **無效**，必須用 FC10

---

## 三、今日發現的重要暫存器

### 已確認意義

| 位址 | 典型值 | 意義 |
|------|--------|------|
| 0x0000 | 0x9011 | 累積電能高字（BCD 32-bit，搭配 0x0001） |
| 0x0001 | 0x0000 | 累積電能低字 |
| 0x0002 | 0xF000 | 狀態旗標（0xF000 = 供電中） |
| 0x0005 | 0x0002 | **裝置 ID（可用 FC10 修改）** ✅ |
| 0x0016 | 0x1350 | 預付餘度（÷100 = 實際度數，此例 = 49.44 度） |

### 修改 ID 的完整範例（Python）

```python
import serial, struct

def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return struct.pack('<H', crc)

def set_meter_id(port, old_id, new_id):
    """把電表 ID 從 old_id 改成 new_id"""
    values = [new_id]
    data = b''.join(struct.pack('>H', v) for v in values)
    payload = bytes([old_id, 0x10]) + struct.pack('>HH', 0x0005, 1) + bytes([2]) + data
    pkt = payload + crc16(payload)

    with serial.Serial(port, 2400, timeout=2) as s:
        s.reset_input_buffer()
        s.write(pkt)
        rx = s.read(8)
        if rx and rx[1] == 0x10:
            print(f'ID {old_id} -> {new_id} 成功')
            return True
        print(f'失敗: {rx.hex()}')
        return False

# 使用範例：把 ID 01 改成 07
# set_meter_id('COM3', old_id=1, new_id=7)
```

---

## 四、不支援的功能

| 功能碼 | 狀態 |
|--------|------|
| FC04（Input Registers） | ❌ 無回應 |
| FC06（Single Register Write） | ❌ 對 reg 0x0005 無效 |
| FC2B（MEI 設備識別） | ❌ 此電表不支援 |

---

## 五、地址空間結構

- 每 **0x80（128）個暫存器** 為一個歷史記錄區塊
- 0x0000~0x007F：當前資料
- 0x0080~0x00FF、0x0100~0x017F … 以此類推：歷史時段資料
- 範圍涵蓋至少 0x0000~0x0FFF
- 高位址（0x1000 以上）映射相同資料，無序號或設備識別資訊

---

## 六、新電表設定 SOP（不需官方介面）

1. 新電表接上 RS485 匯流排
2. 掃描 ID（預設通常是 01 或 255）：
   ```
   對 ID 01~10 發 FC03，有回應的就是新電表
   ```
3. 用 FC10 寫 reg 0x0005 改成目標 ID
4. 確認：用新 ID 發 FC03，`reg 0x0005` 值應等於新 ID
5. 完成，CC2055 後台由 DAE 那邊加對應 ID 即可

---

## 七、CC2055 雲端閘道資訊（今日調查）

| 項目 | 值 |
|------|----|
| IP | 192.168.100.102 |
| Web 介面 | http://192.168.100.102 |
| 預設帳密 | admin / admin |
| 後台伺服器 | clh25.dae.tw |
| 端點 | /device-connect.php |
| 輪詢 ID 清單 | 01~06（固定，由 DAE 後台管理） |
| 架構 | 每 3 秒向 DAE 後台拉任務，每 30 秒回報電能 |

> ⚠️ 2026-03-17 操作中加密金鑰被清空，需聯絡 DAE 客服重設
