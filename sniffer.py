import serial
import time
import os
import sys

# --- 配置區 ---
PORT = 'COM3'
BAUD = 2400
LOG_FILE = 'modbus_raw_log.txt'
# 判定封包間隔的超時時間 (秒)
# 在 2400 bps 下，t3.5 約為 16ms，這裡設定 100ms 較為保險
PACKET_TIMEOUT = 0.1 

class ModbusSniffer:
    def __init__(self):
        self.cleanup_port()
        try:
            self.ser = serial.Serial(PORT, BAUD, timeout=0.01)
            print(f"[*] 嗅探器啟動: {PORT} ({BAUD} bps)")
            print(f"[*] 紀錄檔將儲存至: {LOG_FILE}")
            print(f"[*] 正在監控數據流... 按 Ctrl+C 停止")
        except Exception as e:
            print(f"[!] 無法開啟串列埠: {e}")
            sys.exit(1)

    def cleanup_port(self):
        # 關閉其他可能佔用串口的 Python 程式
        my_pid = os.getpid()
        cmd = f'taskkill /F /FI "IMAGENAME eq python.exe" /FI "PID ne {my_pid}" 2>NUL'
        os.system(cmd)
        time.sleep(0.5)

    def format_hex(self, data):
        return " ".join(f"{b:02X}" for b in data)

    def run(self):
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f"\n--- Sniffing Start: {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            
            buffer = bytearray()
            last_receive_time = time.time()
            
            while True:
                try:
                    if self.ser.in_waiting > 0:
                        chunk = self.ser.read(self.ser.in_waiting)
                        buffer.extend(chunk)
                        last_receive_time = time.time()
                    else:
                        # 檢查是否超時 (代表一個封包結束)
                        if len(buffer) > 0 and (time.time() - last_receive_time > PACKET_TIMEOUT):
                            self.process_packet(buffer, f)
                            buffer = bytearray()
                        time.sleep(0.01)
                except KeyboardInterrupt:
                    print("\n[*] 停止嗅探。")
                    break
                except Exception as e:
                    print(f"\n[!] 錯誤: {e}")
                    break

    def process_packet(self, packet, log_file):
        timestamp = time.strftime('%H:%M:%S')
        hex_data = self.format_hex(packet)
        length = len(packet)
        
        # 簡單的邏輯解析
        desc = "Unknown"
        if length >= 3:
            slave_id = packet[0]
            func_code = packet[1]
            
            if func_code == 0x03:
                if length == 8:
                    desc = f"MASTER -> ID {slave_id:02d} (Read Request)"
                elif packet[2] == (length - 5): # 字節計數檢查
                    desc = f"ID {slave_id:02d} -> MASTER (Read Response)"
            elif func_code == 0x06:
                desc = f"Control: ID {slave_id:02d} (Write Single Register)"
            elif func_code == 0x10:
                desc = f"Control: ID {slave_id:02d} (Write Multiple Registers)"

        output = f"[{timestamp}] {desc:<35} | {hex_data}"
        print(output)
        log_file.write(output + "\n")
        log_file.flush()

if __name__ == "__main__":
    sniffer = ModbusSniffer()
    sniffer.run()
