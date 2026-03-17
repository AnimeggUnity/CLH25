import serial
import time
import os
import csv
import re

# --- 配置區 ---
PORT = 'COM3'
BAUD = 2400
LOG_FILE = 'diag_id02_tracker.csv'
TARGET_ID = 2

class DiagnosticTracker:
    def __init__(self):
        self.cleanup_port()
        try:
            self.ser = serial.Serial(PORT, BAUD, timeout=0.01)
            print(f"[*] 診斷紀錄器啟動: 針對 ID {TARGET_ID:02d}")
            print(f"[*] 數據將儲存至: {LOG_FILE}")
            
            # 初始化 CSV
            if not os.path.exists(LOG_FILE):
                with open(LOG_FILE, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(['Timestamp', 'Event', 'SlaveID', 'Function', 'RawData', 'kWh', 'Balance', 'Info'])
        except Exception as e:
            print(f"[!] 無法開啟串列埠: {e}")
            exit(1)

    def cleanup_port(self):
        my_pid = os.getpid()
        cmd = f'taskkill /F /FI "IMAGENAME eq python.exe" /FI "PID ne {my_pid}" 2>NUL'
        os.system(cmd)
        time.sleep(0.5)

    def log_to_csv(self, event, sid, func, raw, kwh=None, bal=None, info=""):
        with open(LOG_FILE, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([time.strftime('%Y-%m-%d %H:%M:%S'), event, sid, func, raw, kwh, bal, info])

    def run(self):
        buffer = bytearray()
        last_receive_time = time.time()
        PACKET_TIMEOUT = 0.1
        
        print("[*] 正在監聽並追蹤 403室 (ID 02)... 按 Ctrl+C 停止")
        
        while True:
            try:
                if self.ser.in_waiting > 0:
                    chunk = self.ser.read(self.ser.in_waiting)
                    buffer.extend(chunk)
                    last_receive_time = time.time()
                else:
                    if len(buffer) > 0 and (time.time() - last_receive_time > PACKET_TIMEOUT):
                        self.process_packet(buffer)
                        buffer = bytearray()
                    time.sleep(0.01)
            except KeyboardInterrupt:
                print("\n[*] 停止診斷。")
                break

    def process_packet(self, packet):
        hex_data = " ".join(f"{b:02X}" for b in packet)
        length = len(packet)
        if length < 3: return

        sid = packet[0]
        func = packet[1]

        # 1. 偵測讀取回應 (Data Update)
        if sid == TARGET_ID and func == 0x03 and length >= 48:
            # 解析 累積度數 (Reg 0, 1)
            # Modbus Resp: [ID][03][Length][Reg0_H][Reg0_L][Reg1_H][Reg1_L]...
            # Reg 0 是 packet[3:5], Reg 1 是 packet[5:7]
            reg0 = (packet[3] << 8) | packet[4]
            reg1 = (packet[5] << 8) | packet[6]
            kwh = ((reg1 << 16) | reg0) / 100.0
            
            # 解析 餘額 (Reg 22, 23 -> packet[47:49], packet[49:51]?)
            # 基於 24 暫存器讀取長度為 48 bytes payload + 5 bytes overhead = 53 bytes
            if length >= 53:
                r22 = (packet[47] << 8) | packet[48]
                r23 = (packet[49] << 8) | packet[50]
                bal = ((r23 << 16) | r22) / 100.0
            else:
                bal = None
            
            print(f"[{time.strftime('%H:%M:%S')}] ID {sid:02d} | kWh: {kwh:.2f} | Bal: {bal}")
            self.log_to_csv('POLL_RESP', sid, '03', hex_data, kwh, bal)

        # 2. 偵測所有控制指令 (關鍵！捕捉歸零或改寫暫存器的行為)
        elif func in [0x05, 0x06, 0x10]:
            target_desc = f"Control ID {sid}"
            info = ""
            if (func == 0x06 or func == 0x10) and length >= 4:
                addr = (packet[2] << 8) | packet[3]
                info = f"Write Addr: {addr:04X}"
                # 偵測對累積度數暫存器 (0x0000, 0x0001) 的寫入行為
                if addr <= 0x0001:
                    info += " [WARNING] 偵測到修改/歸零累積度數指令!!"
                    print(f"\n{'!'*50}\n[CRITICAL] 偵測到度數改寫指令: {hex_data}\n{'!'*50}")
            
            print(f"[{time.strftime('%H:%M:%S')}] {target_desc:<20} | Func: {func:02X} | {info}")
            self.log_to_csv('WRITE_CMD', sid, f'{func:02X}', hex_data, info=info)

if __name__ == "__main__":
    tracker = DiagnosticTracker()
    tracker.run()
