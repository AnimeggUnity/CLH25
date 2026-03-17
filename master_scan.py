import serial
import time
import sys

def calculate_crc(data):
    crc = 0xFFFF
    for pos in data:
        crc ^= pos
        for i in range(8):
            if (crc & 1) != 0:
                crc >>= 1
                crc ^= 0xA001
            else:
                crc >>= 1
    return crc.to_bytes(2, 'little')

def master_scan(port='COM3'):
    # 聚焦於低速區段，這是許多舊型電表或感測器的預設值
    bauds = [4800, 2400, 1200, 9600]
    parities = [serial.PARITY_NONE, serial.PARITY_EVEN, serial.PARITY_ODD]
    stops = [1, 2]
    slave_ids = [1, 2, 3, 5, 8, 10, 16] 
    
    print(f"--- 啟動超級盲掃模式: {port} ---")
    
    for b in bauds:
        for p_val in parities:
            for s in stops:
                p_name = {serial.PARITY_NONE: 'N', serial.PARITY_EVEN: 'E', serial.PARITY_ODD: 'O'}[p_val]
                print(f"\n[掃描中: {b} bps, {p_name}, {s} stopbits]")
                
                try:
                    with serial.Serial(port, b, parity=p_val, stopbits=s, timeout=0.6) as ser:
                        for s_id in slave_ids:
                            # 嘗試讀取暫存器 00、01 或 0x64 (常見起始位址)
                            for addr in [0x00, 0x01]:
                                req = bytearray([s_id, 0x03, 0x00, addr, 0x00, 0x01])
                                req += calculate_crc(req)
                                
                                ser.write(req)
                                ser.flush()
                                
                                # 等待回應，給予較長的 read 緩衝
                                resp = ser.read(15) 
                                if resp:
                                    # 檢查是否符合基本的 Modbus 回應結構 (ID, FC, LEN...)
                                    if len(resp) >= 5 and resp[0] == s_id and resp[1] == 0x03:
                                        print(f"!!! 命中正確參數 !!!")
                                        print(f"波特率: {b}, 校驗: {p_name}, 停止位: {s}, ID: {s_id}")
                                        print(f"正確回應: {resp.hex(' ').upper()}")
                                        return
                                    else:
                                        print(f"偵測到活動 (ID:{s_id:2d}): {resp.hex(' ').upper()}", end='\r')
                except Exception as e:
                    print(f"跳過埠錯誤: {e}")
    
    print("\n\n盲掃結束，未發現完美匹配的 Modbus 回應。")

if __name__ == "__main__":
    master_scan()

