import serial
import time
import os
import re
import msvcrt
from collections import deque

from rich.console import Console
from rich.table import Table
from rich.layout import Layout
from rich.panel import Panel
from rich.live import Live
from rich.text import Text
from rich import box

# --- 配置區 ---
PORT = 'COM3'
BAUD = 2400
ACTIVE_MODE = True
AVOIDANCE_TIME = 10

OFFSETS = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0}

ROOM_MAP = {
    1: "303室",
    2: "403室",
    3: "402室",
    4: "401室",
    5: "302室",
    6: "301室"
}

STATUS_COLOR = {
    "供電": "green",
    "預付": "cyan",
    "欠費": "yellow",
    "預關": "dark_orange",
    "強關": "red",
    "斷電": "bright_black",
    "---": "bright_black",
}

MAX_LOG_LINES = 12

console = Console()


class PowerMonitor:
    def __init__(self):
        self.log_lines = deque(maxlen=MAX_LOG_LINES)
        self.cleanup_port()
        try:
            self.ser = serial.Serial(PORT, BAUD, timeout=0.1)
            self.log(f"[bold green][CONNECTED][/] 已連接到 {PORT} ({BAUD} bps)")
        except Exception as e:
            console.print(f"[red][ERROR] 連接失敗: {e}[/]")
            exit()

        self.history = {}
        self.latest_data = {i: {
            'kwh': 0.0,
            'watts': 0,
            'status': '---',
            'is_est': True,
            'balance': 0.0,
        } for i in range(1, 7)}
        self.last_bus_activity = time.time()
        self.last_external_activity = time.time()
        self.polls_in_gap = 0
        self.is_self_polling = False
        self.active_id = 1

    def log(self, msg):
        ts = time.strftime('%H:%M:%S')
        self.log_lines.append(f"[bright_black]{ts}[/]  {msg}")

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
        cmd_base = bytes([mid, 0x03, 0x00, 0x00, 0x00, 0x18])
        crc = self.calculate_crc(cmd_base)
        try:
            self.ser.write(cmd_base + crc)
        except Exception as e:
            self.log(f"[red][ERROR] 發送失敗: {e}[/]")

    def send_control_command(self, mid, address, value):
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
                    self.log(f"[green][SUCCESS][/] ID {mid:02d} 控制成功! 回傳: {resp_hex}")
                else:
                    self.log(f"[yellow][NOTICE][/] ID {mid:02d} 非標準回傳: {resp_hex}")
            else:
                self.log(f"[yellow][TIMEOUT][/] ID {mid:02d} 未回傳控制確認")
        except Exception as e:
            self.log(f"[red][ERROR] 控制指令失敗: {e}[/]")

    def send_recharge_command(self, mid, kwh):
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
                    self.log(f"[green][SUCCESS][/] ID {mid:02d} 儲值成功! 設定為 {kwh} 度")
                else:
                    self.log(f"[yellow][NOTICE][/] ID {mid:02d} 儲值回傳異常: {resp.hex().upper()}")
            else:
                self.log(f"[yellow][TIMEOUT][/] ID {mid:02d} 儲值未回傳確認")
        except Exception as e:
            self.log(f"[red][ERROR] 儲值指令失敗: {e}[/]")

    def parse_hex_stream(self, hex_str):
        pattern = r"([0-9A-F]{2})0330([0-9A-F]{96})"
        matches = list(re.finditer(pattern, hex_str))
        if not matches:
            return False, 0

        found_new = False
        last_match_end = 0

        for match in matches:
            try:
                m_id = int(match.group(1), 16)
                if not (1 <= m_id <= 10):
                    continue
                payload = match.group(2)
                regs = [payload[i:i+4] for i in range(0, 96, 4)]
                if len(regs) < 24:
                    continue

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
                if mode in ["強關", "斷電"]:
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
                }
                found_new = True
                last_match_end = match.end()
            except Exception:
                continue

        return found_new, last_match_end

    def build_layout(self):
        # 上方：電表數據表格
        table = Table(
            box=box.ROUNDED,
            show_header=True,
            header_style="bold white",
            title=f"[bold cyan]台科電 RS-485 建物電力監控系統[/]  [bright_black]{time.strftime('%H:%M:%S')}[/]",
            title_justify="center",
            expand=True,
        )
        table.add_column("ID", justify="center", width=4)
        table.add_column("房號", justify="center", width=6)
        table.add_column("狀態", justify="center", width=6)
        table.add_column("累積度數 (kWh)", justify="right", width=16)
        table.add_column("即時功率 (W)", justify="right", width=14)
        table.add_column("餘額 (kWh)", justify="right", width=12)

        for mid in sorted(range(1, 7), key=lambda i: ROOM_MAP.get(i, "")):
            d = self.latest_data[mid]
            color = STATUS_COLOR.get(d['status'], "white")
            status_text = Text(d['status'], style=color)

            kwh_text = Text(f"{d['kwh']:>12.2f}", style="white")

            if d['watts'] == 0:
                watts_text = Text("0 W", style="bright_black")
            elif d['is_est']:
                watts_text = Text(f"~{d['watts']} W", style="yellow")
            else:
                watts_text = Text(f"{d['watts']} W", style="bright_green")

            if d['status'] in ["預付", "欠費", "預關"]:
                bal_text = Text(f"{d['balance']:>10.2f}", style="cyan")
            else:
                bal_text = Text("---", style="bright_black")

            table.add_row(
                f"{mid:02d}",
                ROOM_MAP.get(mid, "未知"),
                status_text,
                kwh_text,
                watts_text,
                bal_text,
            )

        # 下方：log 面板
        mode_str = "[green]智慧避讓模式[/]" if ACTIVE_MODE else "[yellow]被動監聽模式[/]"
        hint = "[bold white]\\[1][/]供電 [bold white]\\[2][/]強關 [bold white]\\[3][/]進預付 [bold white]\\[4][/]移預付 [bold white]\\[5][/]預關 [bold white]\\[6][/]預開 [bold white]\\[7][/]儲值   (ID 02 / 403室)"
        log_content = "\n".join(self.log_lines) if self.log_lines else "[bright_black]等待資料...[/]"

        log_panel = Panel(
            log_content,
            title=f"[bold]事件記錄[/]  {mode_str}",
            subtitle=hint,
            border_style="bright_black",
            padding=(0, 1),
        )

        layout = Layout()
        layout.split_column(
            Layout(table, name="table", ratio=2),
            Layout(log_panel, name="log", ratio=1),
        )
        return layout

    def run(self):
        buffer = ""
        self.log("[cyan][INFO][/] 系統啟動，監聽串列埠中...")

        with Live(self.build_layout(), refresh_per_second=4, screen=True, console=console) as live:
            while True:
                try:
                    # 1. 監聽與讀取
                    if self.ser.in_waiting:
                        current_time = time.time()
                        if not self.is_self_polling:
                            self.last_external_activity = current_time
                            if self.polls_in_gap > 0:
                                self.log("[cyan][DEBUG][/] 偵測到 Master 重啟，重置計數器")
                            self.polls_in_gap = 0
                        self.last_bus_activity = current_time

                        chunk = self.ser.read(self.ser.in_waiting).hex().upper()
                        buffer += chunk
                        if len(buffer) > 4000:
                            buffer = buffer[-2000:]

                        success, end_pos = self.parse_hex_stream(buffer)
                        if success:
                            buffer = buffer[end_pos:]
                            live.update(self.build_layout())

                    # 2. 智慧避讓輪詢
                    if ACTIVE_MODE:
                        idle_time = time.time() - self.last_external_activity
                        should_poll = False
                        if idle_time > 20 and self.polls_in_gap == 0:
                            self.log(f"[cyan][INFO][/] 寂靜 20s，啟動第一波主動更新...")
                            should_poll = True
                        elif idle_time > 40 and self.polls_in_gap == 1:
                            self.log(f"[cyan][INFO][/] 寂靜 40s，啟動第二波主動更新...")
                            should_poll = True

                        if should_poll:
                            self.is_self_polling = True
                            for mid in range(1, 7):
                                self.send_poll_command(mid)
                                time.sleep(0.4)
                                if self.ser.in_waiting:
                                    chunk = self.ser.read(self.ser.in_waiting).hex().upper()
                                    buffer += chunk
                                    success, end_pos = self.parse_hex_stream(buffer)
                                    if success:
                                        buffer = buffer[end_pos:]
                            self.polls_in_gap += 1
                            self.is_self_polling = False
                            self.last_bus_activity = time.time()
                            live.update(self.build_layout())

                    # 3. 鍵盤控制 (ID 02 / 403室)
                    if msvcrt.kbhit():
                        key = msvcrt.getch().decode('utf-8', errors='ignore')
                        if time.time() - self.last_bus_activity > 2:
                            if key == '1':
                                self.log("[magenta][USER][/] 403室 → 開啟供電")
                                self.send_control_command(2, 0x0002, 0xFF00)
                            elif key == '2':
                                self.log("[magenta][USER][/] 403室 → 強制關閉")
                                self.send_control_command(2, 0x0002, 0x0000)
                            elif key == '3':
                                self.log("[magenta][USER][/] 403室 → 進入預付模式")
                                self.send_control_command(2, 0x000B, 0xFF00)
                            elif key == '4':
                                self.log("[magenta][USER][/] 403室 → 切回普通模式")
                                self.send_control_command(2, 0x000B, 0x0000)
                            elif key == '5':
                                status = self.latest_data[2]['status']
                                if status in ["預付", "欠費", "預關"]:
                                    self.log("[magenta][USER][/] 403室 → 預付模式暫時斷電")
                                    self.send_control_command(2, 0x0052, 0x0000)
                                else:
                                    self.log(f"[red][ERROR][/] 狀態 '{status}' 不支援 [5]")
                            elif key == '6':
                                status = self.latest_data[2]['status']
                                if status in ["預付", "欠費", "預關"]:
                                    self.log("[magenta][USER][/] 403室 → 預付模式恢復供電")
                                    self.send_control_command(2, 0x0052, 0xFF00)
                                else:
                                    self.log(f"[red][ERROR][/] 狀態 '{status}' 不支援 [6]")
                            elif key == '7':
                                # 暫停 Live 以便使用 input()
                                live.stop()
                                try:
                                    val_str = input("\n>>> 請輸入 403室 欲設定的度數 (Enter 取消): ").strip()
                                    if val_str:
                                        kwh_to_set = float(val_str)
                                        self.send_recharge_command(2, kwh_to_set)
                                    else:
                                        self.log("[yellow][CANCEL][/] 儲值已取消")
                                except ValueError:
                                    self.log("[red][ERROR][/] 輸入格式錯誤，請輸入數字")
                                live.start()
                            live.update(self.build_layout())

                    # 定時刷新時間顯示
                    live.update(self.build_layout())
                    time.sleep(0.01)

                except Exception as e:
                    self.log(f"[red][LOOP ERROR][/] {e}")
                    time.sleep(1)


if __name__ == "__main__":
    try:
        monitor = PowerMonitor()
        monitor.run()
    except KeyboardInterrupt:
        console.print("\n[yellow][INFO] 使用者停止監控。[/]")
    except Exception as e:
        console.print(f"[red][CRITICAL ERROR] {e}[/]")
