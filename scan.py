import serial
import time
import os
import re
import msvcrt    # Windows 原生鍵盤掃描

# --- 配置區 ---
PORT = 'COM3'
BAUD = 2400  
# 是否開啟智慧避讓主動輪詢 (在空檔發送指令)
ACTIVE_MODE = True 
AVOIDANCE_TIME = 10  # 避讓時間 (秒)

OFFSETS = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0}

ROOM_MAP = {
    1: "303室",
    2: "403室",
    3: "402室",
    4: "401室",
    5: "302室",
    6: "301室"
}

class PowerMonitor:
    def __init__(self):
        self.cleanup_port()
        try:
            self.ser = serial.Serial(PORT, BAUD, timeout=0.1)
            print(f"[CONNECTED] 已連接到 {PORT} ({BAUD} bps)")
        except Exception as e:
            print(f"[ERROR] 連接失敗: {e}")
            exit()
            
        self.history = {} 
        self.latest_data = {i: {
            'kwh': 0.0, 
            'watts': 0, 
            'status': '---', 
            'is_est': True,
            'balance': 0.0,
            'raw_regs': '--'
        } for i in range(1, 7)}
        self.last_bus_activity = time.time()  # 最近一次總線有任何活動
        self.last_external_activity = time.time() # 最近一次外部(Master)活動
        self.polls_in_gap = 0                 # 在目前的空檔中已執行的掃描次數
        self.is_self_polling = False          # 是否由本程式主導通訊中
        self.active_id = 1                    # 當前輪詢對象

    def cleanup_port(self):
        my_pid = os.getpid()
        try:
            parent_pid = os.getppid()
        except:
            parent_pid = 0
        cmd = f'taskkill /F /FI "IMAGENAME eq python.exe" /FI "PID ne {my_pid}" /FI "PID ne {parent_pid}" 2>NUL'
        os.system(cmd)
        time.sleep(1)

    def calculate_crc(self, data):
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

    def send_poll_command(self, mid):
        """主動發送讀取指令"""
        # 指令格式: [ID] 03 00 00 00 18 [CRC]
        cmd_base = bytes([mid, 0x03, 0x00, 0x00, 0x00, 0x18])
        crc = self.calculate_crc(cmd_base)
        full_cmd = cmd_base + crc
        try:
            self.ser.write(full_cmd)
        except Exception as e:
            print(f"[ERROR] 發送失敗: {e}")

    def send_control_command(self, mid, address, value):
        """發送 Function 05 控制指令 (Write Single Coil)"""
        addr_h, addr_l = (address >> 8) & 0xFF, address & 0xFF
        val_h, val_l = (value >> 8) & 0xFF, value & 0xFF
        
        cmd_base = bytes([mid, 0x05, addr_h, addr_l, val_h, val_l])
        crc = self.calculate_crc(cmd_base)
        full_cmd = cmd_base + crc
        try:
            self.ser.write(full_cmd)
            time.sleep(0.3)
            if self.ser.in_waiting:
                resp = self.ser.read(self.ser.in_waiting)
                resp_hex = " ".join(f"{b:02X}" for b in resp).upper()
                if resp == full_cmd:
                    print(f"\n[SUCCESS] ID {mid:02d} 控制成功! 設備回傳確認: {resp_hex}")
                else:
                    print(f"\n[NOTICE] ID {mid:02d} 回傳非標準確認: {resp_hex}")
            else:
                print(f"\n[TIMEOUT] ID {mid:02d} 未回傳控制確認訊號")
        except Exception as e:
            print(f"[ERROR] 控制指令發送失敗: {e}")

    def send_recharge_command(self, mid, kwh):
        """發送 Function 10 儲值指令 (設定剩餘度數)"""
        value = int(kwh * 100)
        low_word = value & 0xFFFF
        high_word = (value >> 16) & 0xFFFF
        
        cmd_base = bytes([
            mid, 0x10, 0x00, 0x16, 0x00, 0x02, 0x04,
            (low_word >> 8) & 0xFF, low_word & 0xFF,
            (high_word >> 8) & 0xFF, high_word & 0xFF
        ])
        crc = self.calculate_crc(cmd_base)
        full_cmd = cmd_base + crc
        try:
            self.ser.write(full_cmd)
            time.sleep(0.5)
            if self.ser.in_waiting:
                resp = self.ser.read(self.ser.in_waiting)
                if len(resp) >= 8 and resp[0:6] == full_cmd[0:6]:
                    print(f"\n[SUCCESS] ID {mid:02d} 儲值成功! 設定為 {kwh} 度")
                else:
                    print(f"\n[NOTICE] ID {mid:02d} 儲值回傳異常: {resp.hex().upper()}")
            else:
                print(f"\n[TIMEOUT] ID {mid:02d} 儲值指令未回傳確認")
        except Exception as e:
            print(f"[ERROR] 儲值指令發送失敗: {e}")

    def parse_hex_stream(self, hex_str):
        pattern = r"([0-9A-F]{2})0330([0-9A-F]{96})"
        matches = list(re.finditer(pattern, hex_str))
        
        if not matches: return False, 0

        found_new = False
        last_match_end = 0
        
        for match in matches:
            try:
                m_id = int(match.group(1), 16)
                if not (1 <= m_id <= 10): continue
                
                payload = match.group(2)
                regs = [payload[i:i+4] for i in range(0, 96, 4)]
                if len(regs) < 24: continue
                
                reg0, reg1, reg2, reg3 = regs[0], regs[1], regs[2], regs[3]
                raw_kwh = int(reg1 + reg0, 16)
                real_kwh = (raw_kwh + OFFSETS.get(m_id, 0)) / 100
                
                v_val = int(reg2[:2], 16)
                a_val = int(reg2[2:], 16)
                est_p = int(v_val * a_val / 400) 
                
                now = time.time()
                calc_p = 0
                if m_id in self.history:
                    dk = real_kwh - self.history[m_id]['kwh']
                    dt = now - self.history[m_id]['time']
                    if dk > 0 and dt > 1:
                        calc_p = int((dk / (dt / 3600)) * 1000)

                if m_id not in self.history or real_kwh != self.history[m_id]['kwh']:
                    self.history[m_id] = {'kwh': real_kwh, 'time': now}

                STATUS_MAP = {
                    "05A1": "供電",
                    "01D1": "預付",
                    "00D0": "欠費",
                    "00C0": "預關",
                    "0080": "強關",
                    "0000": "斷電"
                }
                mode = STATUS_MAP.get(reg3, f"未知({reg3})")

                is_est = True
                if mode in ["強關", "OFF"]:
                    final_p = 0
                    is_est = False
                else:
                    if calc_p > 0:
                        final_p = calc_p
                        is_est = False
                    else:
                        final_p = est_p
                        is_est = True
                        
                    if is_est and final_p < 10:
                        final_p = 0
                
                reg22 = regs[22]
                reg23 = regs[23]
                balance = (int(reg23, 16) << 16) + int(reg22, 16)
                real_balance = balance / 100.0

                self.latest_data[m_id] = {
                    'kwh': real_kwh,
                    'watts': final_p,
                    'status': mode,
                    'is_est': is_est,
                    'balance': real_balance,
                    'raw_regs': f"kWh:{reg0}_{reg1} | Stat:{reg3} | Bal:{reg22}_{reg23}"
                }
                found_new = True
                last_match_end = match.end()
            except Exception:
                continue
        
        return found_new, last_match_end

    def display_report(self):
        print(f"======================================================================")
        print(f"       台科電 RS-485 建物電力監控系統 ({time.strftime('%H:%M:%S')})")
        print(f"======================================================================")
        print(f"ID | 房號  | 狀態 | 累積度數 (kWh) | 即時功率 (Watts) | 餘額 (kWh) ")
        print(f"----------------------------------------------------------------------")
        for mid in range(1, 7):
            d = self.latest_data[mid]
            room = ROOM_MAP.get(mid, "未知 ")
            p_num = d['watts']
            is_est = d['is_est']
            prefix = "~" if (is_est and p_num > 0) else " "
            p_display = f"{prefix}{p_num} W" if p_num >= 0 else "---"
            bal = f"{d['balance']:>10.2f}" if d['status'] in ["預付", "欠費", "預關"] else "    ---   "
            print(f"{mid:02d} | {room} | {d['status']} | {d['kwh']:>12.2f} | {p_display:>14} | {bal}")
        print(f"----------------------------------------------------------------------")
        print(f"[*] ID 02 (403房) 測試: 按 [1]供電 [2]強關 [3]進預付 [4]移預付 [5]預關 [6]預開 [7]儲值")
        mode_str = "智慧避讓模式" if ACTIVE_MODE else "被動監聽模式"
        print(f"[INFO] 模式: {mode_str} | 避讓空檔: {AVOIDANCE_TIME}s")

    def run(self):
        buffer = ""
        print("[INFO] 系統切換至智慧雙重避讓模式，正在監聽串列埠...")
        while True:
            try:
                # 1. 監聽與讀取處理
                if self.ser.in_waiting:
                    current_time = time.time()
                    
                    if not self.is_self_polling:
                        self.last_external_activity = current_time
                        if self.polls_in_gap > 0:
                            print(f"[DEBUG] 偵測到 Master 重啟活動，重置計數器")
                        self.polls_in_gap = 0
                    
                    self.last_bus_activity = current_time
                    
                    chunk = self.ser.read(self.ser.in_waiting).hex().upper()
                    buffer += str(chunk)
                    
                    if len(buffer) > 4000:
                        buffer = buffer[-2000:]
                    
                    success, end_pos = self.parse_hex_stream(buffer)
                    if success:
                        self.display_report()
                        buffer = buffer[end_pos:]
                
                # 2. 雙重突發避讓邏輯 (20s / 40s)
                if ACTIVE_MODE:
                    idle_time = time.time() - self.last_external_activity
                    
                    should_poll = False
                    if idle_time > 20 and self.polls_in_gap == 0:
                        print(f"\n[INFO] 寂靜 20s (與上波間隔約 {idle_time:.1f}s)，啟動第一波主動更新...")
                        should_poll = True
                    elif idle_time > 40 and self.polls_in_gap == 1:
                        print(f"\n[INFO] 寂靜 40s (與上波間隔約 {idle_time:.1f}s)，啟動第二波主動更新...")
                        should_poll = True
                        
                    if should_poll:
                        self.is_self_polling = True
                        for mid in range(1, 7):
                            self.send_poll_command(mid)
                            time.sleep(0.4)
                            if self.ser.in_waiting:
                                chunk = self.ser.read(self.ser.in_waiting).hex().upper()
                                buffer += str(chunk)
                                success, end_pos = self.parse_hex_stream(buffer)
                                if success:
                                    self.display_report()
                                    buffer = buffer[end_pos:]
                        
                        self.polls_in_gap += 1
                        self.is_self_polling = False
                        self.last_bus_activity = time.time()
                    
                # 3. 鍵盤控制監測 (專門測試 ID 02)
                if msvcrt.kbhit():
                    key = msvcrt.getch().decode('utf-8')
                    if time.time() - self.last_bus_activity > 2:
                        if key == '1':
                            print("\n[USER] 發送: 403室 開啟供電")
                            self.send_control_command(2, 0x0002, 0xFF00)
                        elif key == '2':
                            print("\n[USER] 發送: 403室 強制關閉")
                            self.send_control_command(2, 0x0002, 0x0000)
                        elif key == '3':
                            print("\n[USER] 發送: 403室 進入預付模式")
                            self.send_control_command(2, 0x000B, 0xFF00)
                        elif key == '4':
                            print("\n[USER] 發送: 403室 切回普通模式")
                            self.send_control_command(2, 0x000B, 0x0000)
                        elif key == '5':
                            status = self.latest_data[2]['status']
                            if status in ["預付", "欠費", "預關"]:
                                print("\n[USER] 發送: 403室 預付模式暫時斷電")
                                self.send_control_command(2, 0x0052, 0x0000)
                            else:
                                print(f"\n[ERROR] 操作失敗! 狀態 '{status}' 不支援 [5]")
                        elif key == '6':
                            status = self.latest_data[2]['status']
                            if status in ["預付", "欠費", "預關"]:
                                print("\n[USER] 發送: 403室 預付模式恢復供電")
                                self.send_control_command(2, 0x0052, 0xFF00)
                            else:
                                print(f"\n[ERROR] 操作失敗! 狀態 '{status}' 不支援 [6]")
                        elif key == '7':
                            print("\n[RECHARGE] 暫停監控，進入儲值模式...")
                            try:
                                val_str = input(">>> 請輸入 403室 欲設定的度數 (直接 Enter 取消): ").strip()
                                if val_str:
                                    kwh_to_set = float(val_str)
                                    self.send_recharge_command(2, kwh_to_set)
                                else:
                                    print("[CANCEL] 儲值已取消")
                            except ValueError:
                                print("[ERROR] 輸入格式錯誤，請輸入數字")
                        
                time.sleep(0.01)

            except Exception as e:
                print(f"[LOOP ERROR] {e}")
                time.sleep(1)
                continue

if __name__ == "__main__":
    try:
        monitor = PowerMonitor()
        monitor.run()
    except KeyboardInterrupt:
        print("\n[INFO] 使用者停止監控。")
    except Exception as e:
        print(f"[CRITICAL ERROR] {e}")