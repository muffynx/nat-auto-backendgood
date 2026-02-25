"""
NAT-AUTO Agent GUI
──────────────────
ต้องการ: pip install customtkinter pystray pillow python-socketio netmiko python-dotenv
"""
from typing import Optional


import customtkinter as ctk
import threading
import time
import os
import sys
import traceback
import queue
import tkinter as tk
from datetime import datetime
from dotenv import load_dotenv, set_key
from concurrent.futures import ThreadPoolExecutor, as_completed
import socketio

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

# ─────────────────────────────────────────────
#   Icon paths
# ─────────────────────────────────────────────
ICONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icons', 'png')

def mk_icon(name: str, color: str = "#ffffff", size: tuple = (16, 16)):
    """Load icons/png/<name>.png and tint all opaque pixels to `color`."""
    try:
        from PIL import Image as _I
        img = _I.open(os.path.join(ICONS_DIR, f"{name}.png")).convert("RGBA")
        r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
        px = img.load()
        for y in range(img.height):
            for x in range(img.width):
                _, _, _, a = px[x, y]
                if a > 10:
                    px[x, y] = (r, g, b, a)
        img = img.resize(size, _I.LANCZOS)
        return ctk.CTkImage(img, img, size=size)
    except Exception:
        return None

# ─────────────────────────────────────────────
#   Load .env
# ─────────────────────────────────────────────
load_dotenv()
ENV_PATH = os.path.join(os.path.dirname(__file__), '.env')

DEFAULT_URL    = os.getenv('VPS_URL')
DEFAULT_KEY    = os.getenv('AGENT_KEY', '')
DEFAULT_WORKERS = int(os.getenv('MAX_WORKERS', '10'))

# ─────────────────────────────────────────────
#   Colors
# ─────────────────────────────────────────────
BG = "#0f172a"
CARD = "#111827"
CARD_ALT = "#1e293b"  # สีสลับบรรทัด Log (ม้าลาย)
BORDER    = "#30363d"
ACCENT = "#5865F2"  
ACCENT2   = "#06b6d4"
GREEN = "#23a559"
RED = "#f23f42"
YELLOW    = "#fbbf24"
TEXT = "#f9fafb"
TEXT_DIM = "#9ca3af"
TS_COLOR = "#38bdf8"  # สีเวลา Timestamp ให้สว่างและชัดขึ้น

# ─────────────────────────────────────────────
#   Fonts  (Segoe UI = Windows built-in, looks premium)
# ─────────────────────────────────────────────
F_BODY    = "Segoe UI"
F_MONO    = "Consolas"

F_TITLE   = (F_BODY, 14, "bold")
F_LABEL   = (F_BODY, 12)
F_SMALL   = (F_BODY, 11)
F_TINY    = (F_BODY, 10)
F_BTN     = (F_BODY, 12, "bold")
F_BTN_SM  = (F_BODY, 11)
F_KEY     = (F_MONO, 14) # ใหญ่ขึ้น
F_LOG     = (F_MONO, 12) # ใหญ่ขึ้น
F_TS      = (F_MONO, 11, "bold") # ทำตัวหนาให้เวลาชัดๆ
# ─────────────────────────────────────────────
#   Agent Core Logic (runs in background thread)
# ─────────────────────────────────────────────

def get_device_driver(device):
    return {
        'device_type': device['device_type'],
        'host':        device['ip_address'],
        'username':    device['username'],
        'password':    device['password'],
        'secret':      device.get('secret', ''),
        'port':        int(device.get('port', 22)),
        'global_delay_factor': 0.5,
        'fast_cli':    True,
        'banner_timeout': 15,
        'timeout':        10,
        'auth_timeout':   15,
    }

def get_backup_commands(device_type):
    """ คืนค่ารายการคำสั่งดึง config และสถานะการทำงาน (Operational State) ตาม vendor """
    dtype = device_type.lower()
    
    # ── Cisco / Aruba (IOS-like) ──
    if "cisco" in dtype or "aruba" in dtype:
        return [
            ("Running Configuration", "show running-config"),
            ("Version & Uptime", "show version"),
            ("Interface Status", "show ip interface brief"),
            ("LLDP Neighbors", "show lldp neighbors detail" if "cisco" not in dtype else "show lldp neighbors"),
            ("CDP Neighbors", "show cdp neighbors detail"),
            ("MAC Address Table", "show mac address-table"),
            ("ARP Table", "show ip arp"),
            ("Routing Table", "show ip route")
        ]
        
    # ── HP / Comware / Huawei ──
    elif "hp" in dtype or "comware" in dtype or "huawei" in dtype:
        return [
            ("Current Configuration", "display current-configuration"),
            ("Version & Uptime", "display version"),
            ("Interface Status", "display ip interface brief"),
            ("LLDP Neighbors", "display lldp neighbor-information verbose"),
            ("MAC Address Table", "display mac-address"),
            ("ARP Table", "display arp"),
            ("Routing Table", "display ip routing-table")
        ]
        
    # ── Juniper ──
    elif "juniper" in dtype:
        return [
            ("Configuration", "show configuration"),
            ("Version & Uptime", "show version"),
            ("Interface Status", "show interfaces terse"),
            ("LLDP Neighbors", "show lldp neighbors"),
            ("ARP Table", "show arp"),
            ("Routing Table", "show route")
        ]
        
    # ── Fortinet ──
    elif "fortinet" in dtype:
        return [
            ("Full Configuration", "show full-configuration"),
            ("System Status", "get system status"),
            ("Interface Status", "get system interface physical"),
            ("ARP Table", "get system arp"),
            ("Routing Table", "get router info routing-table all")
        ]
        
    # ── Default Fallback ──
    return [("Running Configuration", "show running-config")]

def task_backup(device):
    from netmiko import ConnectHandler
    import time
    try:
        net_connect = ConnectHandler(**get_device_driver(device))
        
        commands = get_backup_commands(device['device_type'])
        full_output = f"=== NETWORK AUDIT BACKUP FOR {device.get('hostname', 'UNKNOWN')} ===\n"
        full_output += f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        
        for section_name, cmd in commands:
            full_output += f"\n{'='*60}\n"
            full_output += f"👉 {section_name} ({cmd})\n"
            full_output += f"{'='*60}\n"
            try:
                out = net_connect.send_command(cmd, read_timeout=90)
                full_output += out + "\n"
            except Exception as e:
                full_output += f"[Error executing command: {str(e)}]\n"
                
        net_connect.disconnect()
        return {'status': 'Success', 'output': full_output}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {'status': 'Failed', 'output': str(e)}

def task_push_config(device, commands):
    from netmiko import ConnectHandler
    hostname = device.get('hostname', 'unknown')
    try:
        flat = []
        if isinstance(commands, str):
            commands = [commands]
        for cmd in commands:
            for sub in str(cmd).split('\n'):
                if sub.strip():
                    flat.append(sub.strip())

        net_connect = ConnectHandler(**get_device_driver(device))
        if device.get('secret'):
            net_connect.enable()
        output = net_connect.send_config_set(flat, read_timeout=90)

        save_out = ''
        dtype = device.get('device_type', '').lower()
        save_cmd = None
        if "cisco" in dtype or "aruba" in dtype:
            save_cmd = "write memory"
        elif "hp" in dtype or "comware" in dtype or "huawei" in dtype:
            save_cmd = "save force"
        if save_cmd:
            save_out = net_connect.send_command(save_cmd, read_timeout=60)

        net_connect.disconnect()
        return {'status': 'Success', 'output': output,
                'save_output': save_out, 'commands_applied': flat}
    except Exception as e:
        traceback.print_exc()
        return {'status': 'Failed', 'output': str(e),
                'save_output': '', 'commands_applied': []}

def task_run_command(device, command):
    from netmiko import ConnectHandler
    try:
        net_connect = ConnectHandler(**get_device_driver(device))
        output = net_connect.send_command(command, read_timeout=120)
        net_connect.disconnect()
        return {'status': 'Success', 'output': output}
    except Exception as e:
        traceback.print_exc()
        return {'status': 'Failed', 'output': str(e)}


# ─────────────────────────────────────────────
#   Agent Thread
# ─────────────────────────────────────────────

class AgentThread(threading.Thread):
    def __init__(self, server_url: str, agent_key: str, max_workers: int,
                 log_queue: queue.Queue, status_callback):
        super().__init__(daemon=True)
        self.server_url      = server_url
        self.agent_key       = agent_key
        self.max_workers     = max_workers
        self.log_q           = log_queue
        self.status_cb       = status_callback
        self.allowed_user    = None
        self._stop_event     = threading.Event()
        self.sio             = socketio.Client(
            reconnection=True,
            reconnection_delay=3,
            reconnection_attempts=999
        )
        self._setup_events()

    def _log(self, icon: str, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_q.put((ts, icon, msg))

    def _setup_events(self):
        sio = self.sio

        @sio.event
        def connect():
            self._log("🔌", f"Connected → Server")
            self.status_cb("connecting", None)
            sio.emit('register_agent', {'agent_key': self.agent_key})

        @sio.event
        def disconnect():
            self._log("⚠️", "Disconnected — retrying...")
            self.status_cb("disconnected", None)
            self.allowed_user = None

        @sio.on('agent_auth_success')
        def on_auth_ok(payload):
            user = payload.get('user')
            self.allowed_user = user
            self._log("✅", f"Authorized as  →  {user}")
            self.status_cb("connected", user)

        @sio.on('agent_auth_failed')
        def on_auth_fail(payload):
            self._log("❌", f"Auth failed: {payload.get('message', '?')}")
            self.status_cb("auth_failed", None)

        @sio.on('execute_task')
        def on_task(payload):
            if not self.allowed_user:
                return
            if payload.get('owner') != self.allowed_user:
                return
            threading.Thread(target=self._handle_task,
                             args=(payload,), daemon=True).start()

    def _handle_task(self, payload):
        task_type = payload.get('type')
        owner     = payload.get('owner')

        # ── BACKUP ────────────────────────────────────
        if task_type == 'backup':
            device = payload.get('device', {})
            hostname = device.get('hostname', '?')
            self._log("💾", f"Backup  {hostname} ...")
            result = task_backup(device)
            status = result['status']
            icon   = "✅" if status == 'Success' else "❌"
            self._log(icon, f"Backup {hostname}  →  {status}")
            self.sio.emit('task_result', {
                'type': task_type, 'status': status,
                'output': result['output'],
                'hostname': hostname,
                'device_id': payload.get('device_id'),
                'owner': owner
            })

        # ── BATCH BACKUP ───────────────────────────────
        elif task_type == 'batch_backup':
            devices = payload.get('devices', [])
            self._log("📦", f"Batch backup  →  {len(devices)} devices")



            # ✅ 1. ส่งสถานะเริ่มต้น (10% Connecting) กลับไปบอกหน้าเว็บก่อนทันที
            for d in devices:
                self.sio.emit('task_result', {
                    'type': 'backup',
                    'status': 'Running',
                    'percent': 10,
                    'msg': 'Connecting...',
                    'hostname': d.get('hostname', '?'),
                    'device_id': d.get('_id'),
                    'owner': owner
                })


                # ✅ 2. เริ่มเปิด Thread เข้าอุปกรณ์จริงๆ
            with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
                futures = {ex.submit(task_backup, d): d for d in devices}
                for fut in as_completed(futures):
                    dev = futures[fut]
                    hostname = dev.get('hostname', '?')
                    try:
                        res    = fut.result()
                        status = res['status']
                        icon   = "✅" if status == 'Success' else "❌"
                        self._log(icon, f"  └ {hostname}  →  {status}")



                        self.sio.emit('task_result', {
                            'type': 'backup', 'status': status,
                            'output': res['output'],
                            'hostname': hostname,
                            'device_id': dev.get('_id'),
                            'owner': owner
                        })
                    except Exception as exc:
                        self._log("❌", f"  └ {hostname}  →  {exc}")
                        self.sio.emit('task_result', {
                            'type': 'backup', 'status': 'Failed',
                            'output': str(exc),
                            'hostname': hostname,
                            'device_id': dev.get('_id'),
                            'owner': owner
                        })

        # ── BATCH CONFIG ───────────────────────────────
        elif task_type == 'batch_config':
            devices  = payload.get('devices', [])
            raw_cmds = payload.get('commands', [])
            if isinstance(raw_cmds, str):
                commands = [c.strip() for c in raw_cmds.split('\n') if c.strip()]
            else:
                commands = raw_cmds
            self._log("⚙️", f"Batch config  →  {len(devices)} devices  |  {len(commands)} cmds")

            summary = {'success': 0, 'failed': 0}
            details = []
            with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
                futures = {ex.submit(task_push_config, d, commands): d for d in devices}
                for fut in as_completed(futures):
                    dev      = futures[fut]
                    hostname = dev.get('hostname', '?')
                    try:
                        res        = fut.result()
                        is_success = res['status'] == 'Success'
                        applied    = res.get('commands_applied', [])
                        save_out   = res.get('save_output', '').strip()
                        icon       = "✅" if is_success else "❌"
                        self._log(icon, f"  └ {hostname}  →  {res['status']}")
                        if is_success:
                            summary['success'] += 1
                            cmd_lines = '\n'.join(f'  {i+1}. {c}' for i, c in enumerate(applied))
                            log = f"Commands Applied ({len(applied)}):\n{cmd_lines}"
                            if save_out:
                                log += f"\nSave: {save_out[:120]}"
                        else:
                            summary['failed'] += 1
                            log = res['output']
                        details.append({
                            'host': hostname,
                            'ip': dev.get('ip_address', ''),
                            'status': 'success' if is_success else 'failed',
                            'commands_applied': applied,
                            'log': log
                        })
                    except Exception as exc:
                        summary['failed'] += 1
                        details.append({'host': hostname, 'ip': '',
                                        'status': 'failed', 'commands_applied': [],
                                        'log': str(exc)})

            self.sio.emit('task_result', {
                'type': 'batch_config',
                'summary': summary,
                'details': details,
                'owner': owner
            })
            self._log("📊", f"Batch done  ✅ {summary['success']}  ❌ {summary['failed']}")

        # ── RUN COMMAND ────────────────────────────────
        elif task_type == 'run_command':
            device  = payload.get('device', {})
            command = payload.get('command', '')
            hostname = device.get('hostname', '?')
            self._log("💻", f"CMD  {hostname}  →  {command[:40]}")
            result = task_run_command(device, command)
            icon   = "✅" if result['status'] == 'Success' else "❌"
            self._log(icon, f"CMD {hostname}  →  {result['status']}")
            self.sio.emit('task_result', {
                'type': task_type, 'status': result['status'],
                'output': result['output'],
                'hostname': hostname,
                'owner': owner
            })

        else:
            self._log("❓", f"Unknown task type: {task_type}")

    def run(self):
        try:
            self.sio.connect(self.server_url, transports=['websocket'])
            while not self._stop_event.is_set():
                time.sleep(0.5)
        except Exception as e:
            self._log("❌", f"Connection error: {e}")
            self.status_cb("disconnected", None)

    def stop(self):
        self._stop_event.set()
        try:
            self.sio.disconnect()
        except Exception:
            pass


# ─────────────────────────────────────────────
#   Main GUI Application
# ─────────────────────────────────────────────

class AgentApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("NAT-AUTO Agent")
        self.geometry("700x640")
        self.minsize(600, 520)
        self.configure(fg_color=BG)
        ctk.set_appearance_mode("dark")

        self._agent_thread: Optional[AgentThread] = None
        self._log_queue    = queue.Queue()
        self._log_rows     = []

        self._build_ui()
        self._poll_log()
        # ตั้ง icon หลัง mainloop เริ่ม
        self.after(200, self._set_window_icon)

        if HAS_TRAY:
            self.protocol("WM_DELETE_WINDOW", self._on_close_to_tray)
            self._init_tray()
        else:
            self.protocol("WM_DELETE_WINDOW", self.destroy)

    # ── Window icon ──────────────────────────────
    def _set_window_icon(self):
        try:
            from PIL import Image, ImageDraw
            import io, tempfile
            from tkinter import PhotoImage

            # หา bundle_dir
            if getattr(sys, 'frozen', False):
                bundle_dir = sys._MEIPASS  # type: ignore
            else:
                bundle_dir = os.path.dirname(os.path.abspath(__file__))

            logo_path = os.path.join(bundle_dir, 'icons', 'logo.jpg')

            if os.path.exists(logo_path):
                img = Image.open(logo_path).convert("RGBA").resize((64, 64))
            else:
                # fallback gradient circle
                img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                d = ImageDraw.Draw(img)
                for r in range(28, 0, -1):
                    t = r / 28
                    rc = int(6   + (59  - 6)   * t)
                    gc = int(182 + (130 - 182) * t)
                    bc = int(212 + (246 - 212) * t)
                    d.ellipse([32-r, 32-r, 32+r, 32+r], fill=(rc, gc, bc, 255))
                d.text((21, 16), "N", fill="white")

            # ─ วิธีที่ทำงานได้บน Windows: บันทึกเป็น .ico temp file
            ico_img = img.resize((32, 32))
            tmp = tempfile.NamedTemporaryFile(suffix='.ico', delete=False)
            ico_img.save(tmp.name, format='ICO', sizes=[(32, 32)])
            tmp.close()
            self.after(0, lambda: self.wm_iconbitmap(tmp.name))

        except Exception as e:
            print(f"Icon error: {e}")


    # ── Build UI ──────────────────────────────
    def _build_ui(self):
        # ── Top status bar ──────────────────────
        top = ctk.CTkFrame(self, fg_color=CARD, corner_radius=0, height=56)
        top.pack(fill="x", side="top")
        top.pack_propagate(False)

        self._dot = ctk.CTkLabel(top, text="", image=mk_icon('server', TEXT_DIM, (20, 20)),
                                  compound="left", fg_color="transparent",
                                  text_color="#374151", width=28)
        self._dot.pack(side="left", padx=(14, 6), pady=10)

        txt_col = ctk.CTkFrame(top, fg_color="transparent")
        txt_col.pack(side="left", pady=8)
        ctk.CTkLabel(txt_col, text="NAT-AUTO Agent",
                      font=(F_BODY, 13, "bold"), text_color=TEXT).pack(anchor="w")
        self._status_label = ctk.CTkLabel(txt_col, text="Offline",
                                           font=F_SMALL, text_color=TEXT_DIM)
        self._status_label.pack(anchor="w")

        self._user_label = ctk.CTkLabel(top, text="",
                                         font=F_SMALL, text_color=ACCENT2)
        self._user_label.pack(side="left", padx=(10, 0), pady=10)

        self._stop_btn = ctk.CTkButton(
            top, text=" Stop", image=mk_icon('circle-stop', RED, (16, 16)),
            compound="left", width=100, height=34,
            fg_color="#2b1515", hover_color="#3d1f1f",
            border_color=RED, border_width=1,
            text_color=RED, font=F_BTN,
            command=self._stop_agent, state="disabled"
        )
        self._stop_btn.pack(side="right", padx=(8, 16), pady=10)

        self._start_btn = ctk.CTkButton(
            top, text=" Start", image=mk_icon('play', "#ffffff", (16, 16)),
            compound="left", width=100, height=34,
            fg_color=ACCENT, hover_color="#2563eb",
            text_color="white", font=F_BTN,
            command=self._start_agent
        )
        self._start_btn.pack(side="right", padx=4, pady=10)

        # Settings toggle button
        self._settings_open = False
        self._settings_btn = ctk.CTkButton(
            top, text=" Settings",
            image=mk_icon('settings', TEXT_DIM, (16, 16)),
            compound="left", width=110, height=34,
            fg_color="transparent", hover_color="#1e293b",
            border_color=BORDER, border_width=1,
            text_color=TEXT_DIM, font=F_BTN_SM,
            command=self._toggle_settings
        )
        self._settings_btn.pack(side="right", padx=(0, 8), pady=10)

       # ── Config panel (hidden by default, toggled by Settings btn) ──
        self._cfg_frame = ctk.CTkFrame(self, fg_color=CARD,
                                        corner_radius=0, border_width=0,
                                        border_color=BORDER)
        # NOTE: ไม่ pack ตอนนี้ — จะ pack/forget เมื่อกด Settings
        self._cfg_frame.grid_columnconfigure(1, weight=1)

        # Agent Key row
        ctk.CTkLabel(self._cfg_frame, text="Agent Key", font=F_LABEL,
                      text_color=TEXT_DIM).grid(row=0, column=0, padx=(20, 10), pady=(14, 8), sticky="w")
        key_frame = ctk.CTkFrame(self._cfg_frame, fg_color="transparent")
        key_frame.grid(row=0, column=1, padx=(0, 20), pady=(14, 8), sticky="ew")
        key_frame.grid_columnconfigure(0, weight=1)

        self._key_entry = ctk.CTkEntry(key_frame, placeholder_text="agk_...",
                                        fg_color="#0d1117", border_color=BORDER,
                                        text_color=YELLOW, font=F_KEY,
                                        show="*", height=34)
        self._key_entry.insert(0, DEFAULT_KEY)
        self._key_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self._show_key_btn = ctk.CTkButton(
            key_frame, text="👁", width=36, height=34,
            fg_color="#1a222e", hover_color="#243040",
            border_color=BORDER, border_width=1,
            text_color=TEXT_DIM, font=(F_BODY, 14),
            command=self._toggle_key_visibility
        )
        self._show_key_btn.grid(row=0, column=1, padx=(0, 8))

        ctk.CTkButton(
            key_frame, text="Save", width=64, height=34,
            fg_color="#1a222e", hover_color="#243040",
            border_color=BORDER, border_width=1,
            text_color=TEXT_DIM, font=F_BTN_SM,
            command=self._save_env
        ).grid(row=0, column=2)

        # separator
        ctk.CTkFrame(self._cfg_frame, height=1, fg_color=BORDER).grid(
            row=1, column=0, columnspan=2, sticky="ew", padx=16
        )

        # Workers row
        ctk.CTkLabel(self._cfg_frame, text="Workers", font=F_LABEL,
                      text_color=TEXT_DIM).grid(row=2, column=0, padx=(20, 10), pady=(8, 14), sticky="w")
        w_right = ctk.CTkFrame(self._cfg_frame, fg_color="transparent")
        w_right.grid(row=2, column=1, padx=(0, 20), pady=(8, 14), sticky="ew")
        w_right.grid_columnconfigure(0, weight=1)

        self._workers_var = ctk.IntVar(value=DEFAULT_WORKERS)
        self._workers_slider = ctk.CTkSlider(
            w_right, from_=1, to=30, number_of_steps=29,
            variable=self._workers_var,
            progress_color=ACCENT, button_color=ACCENT2
        )
        self._workers_slider.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkLabel(w_right, textvariable=self._workers_var,
                      text_color=ACCENT2, font=(F_BODY, 12, "bold"), width=32
        ).grid(row=0, column=1)
        ctk.CTkLabel(w_right, text="งาน parallel สูงสุด",
                      font=F_TINY, text_color="#4b5563"
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        # ── Log panel ───────────────────────────
        self._log_outer = ctk.CTkFrame(self, fg_color=CARD,
                                        corner_radius=12, border_width=1,
                                        border_color=BORDER)
        log_outer = self._log_outer
        log_outer.pack(fill="both", expand=True, padx=16, pady=12)

        log_header = ctk.CTkFrame(log_outer, fg_color="transparent")
        log_header.pack(fill="x", padx=14, pady=(10, 4))
        ctk.CTkLabel(log_header, text=" Task Log",
                      image=mk_icon('logs', ACCENT2, (16, 16)), compound="left",
                      font=(F_BODY, 13, "bold"), text_color=TEXT).pack(side="left")
        ctk.CTkButton(
            log_header, text="Clear", width=60, height=26,
            fg_color="transparent", hover_color="#1a222e",
            border_color=BORDER, border_width=1,
            text_color=TEXT_DIM, font=F_BTN_SM,
            command=self._clear_log
        ).pack(side="right")

        self._log_frame = ctk.CTkScrollableFrame(
            log_outer, fg_color="transparent", corner_radius=0
        )
        self._log_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self._tip = ctk.CTkLabel(
            self, text="ตั้งค่า Server URL และ Agent Key แล้วกด  ▶  Start",
            font=F_SMALL, text_color=TEXT_DIM
        )
        self._tip.pack(pady=(0, 10))

    # ── Key visibility toggle ─────────────────
    def _toggle_key_visibility(self):
        if self._key_entry.cget("show") == "*":
            self._key_entry.configure(show="")
            self._show_key_btn.configure(text="🙈")
        else:
            self._key_entry.configure(show="*")
            self._show_key_btn.configure(text="👁")

    # ── Save to .env ─────────────────────────
    def _save_env(self):
        key = self._key_entry.get().strip()
        if key:
            set_key(ENV_PATH, 'AGENT_KEY', key)
        self._add_log_row("💾", "Settings saved to .env", color=ACCENT2)

    # ── Start agent ───────────────────────────
    # ── Settings toggle ────────────────────────
    def _toggle_settings(self):
        self._settings_open = not self._settings_open
        if self._settings_open:
            # แทรกระหว่าง top bar กับ log panel
            self._cfg_frame.pack(fill="x", padx=16, pady=(8, 0),
                                  before=self._log_outer)
            self._settings_btn.configure(
                fg_color="#1e293b",
                border_color=ACCENT2,
                text_color=ACCENT2,
                image=mk_icon('settings', ACCENT2, (16, 16))
            )
        else:
            self._cfg_frame.pack_forget()
            self._settings_btn.configure(
                fg_color="transparent",
                border_color=BORDER,
                text_color=TEXT_DIM,
                image=mk_icon('settings', TEXT_DIM, (16, 16))
            )

    def _start_agent(self):

        url = DEFAULT_URL  # ✅ ใช้ตัวแปรคงที่จากหัวไฟล์แทน
        key = self._key_entry.get().strip()
        
        if not key: # เช็คแค่ Key อย่างเดียว
            self._add_log_row("⚠️", "กรุณาใส่ Agent Key ก่อน", color=YELLOW)
            return

        workers = self._workers_var.get()
        self._add_log_row("🚀", "Starting agent...", color=ACCENT) # ซ่อน URL ใน Log ด้วย
        self._start_btn.configure(state="disabled")
        self._stop_btn.configure(state="normal")
        self._key_entry.configure(state="disabled") # ปิดแค่ช่อง Key

        self._agent_thread = AgentThread(
            server_url=url, agent_key=key, max_workers=workers,
            log_queue=self._log_queue,
            status_callback=self._on_status_change
        )
        self._agent_thread.start()

    # ── Stop agent ────────────────────────────
    def _stop_agent(self):
        if self._agent_thread:
            self._agent_thread.stop()
            self._agent_thread = None
        self._add_log_row("⏹", "Agent stopped", color=RED)
        self._on_status_change("disconnected", None)
        self._start_btn.configure(state="normal")
        self._stop_btn.configure(state="disabled")
        self._key_entry.configure(state="normal") # เปิดแค่ช่อง Key

    # ── Status update (called from agent thread) ──
    def _on_status_change(self, state: str, user: Optional[str]):
        def _update():
            if state == "connected":
                self._dot.configure(text_color=GREEN)
                self._status_label.configure(text="Connected", text_color=GREEN)
                self._user_label.configure(text=f"({user})")
                self._tip.configure(text=f"🟢 Ready — receiving tasks for {user}")
            elif state == "connecting":
                self._dot.configure(text_color=YELLOW)
                self._status_label.configure(text="Registering…", text_color=YELLOW)
                self._user_label.configure(text="")
            elif state == "auth_failed":
                self._dot.configure(text_color=RED)
                self._status_label.configure(text="Auth Failed", text_color=RED)
                self._user_label.configure(text="")
                self._tip.configure(text="❌ Invalid Agent Key — check and try again")
            else:  # disconnected
                self._dot.configure(text_color="#374151")
                self._status_label.configure(text="Offline", text_color=TEXT_DIM)
                self._user_label.configure(text="")
                self._tip.configure(text="Agent offline")
        self.after(0, _update)

    # ── Log polling ────────────────────────────
    def _poll_log(self):
        while not self._log_queue.empty():
            ts, icon, msg = self._log_queue.get_nowait()
            self._add_log_row(icon, msg, ts=ts)
        self.after(300, self._poll_log)

    def _add_log_row(self, icon: str, msg: str, ts: Optional[str] = None,
                     color: str = TEXT):
        if ts is None:
            ts = datetime.now().strftime("%H:%M:%S")

        ICON_MAP = {
            "✅": ("check",                GREEN),
            "❌": ("circle-x",             RED),
            "⚠️": ("circle-x",             YELLOW),
            "🔌": ("plug",                 ACCENT),
            "💻": ("terminal",             ACCENT2),
            "⚙️": ("settings",             TEXT_DIM),
            "❓": ("circle-question-mark", TEXT_DIM),
            "📎": ("settings",             ACCENT2),
            "💾": ("save",                 ACCENT2),
            "📦": ("database-backup",      ACCENT),
            "📊": ("logs",                 ACCENT2),
            "👁": ("eys",                  TEXT_DIM),
            "🙈": ("eys-off",              TEXT_DIM),
            "🚀": ("rocket",               GREEN),
            "⏹": ("circle-stop",         RED),
            "🟢": ("circle-check",         GREEN),
        }

        row = ctk.CTkFrame(self._log_frame, fg_color="transparent")
        row.pack(fill="x", pady=1)

        ctk.CTkLabel(row, text=ts, font=F_TS,
                      text_color=TEXT_DIM, width=62, anchor="e").pack(side="left")

        icon_file, icon_color = ICON_MAP.get(icon, (None, TEXT_DIM))
        ico_img = mk_icon(icon_file, icon_color, (15, 15)) if icon_file else None

        if ico_img:
            ctk.CTkLabel(row, text="", image=ico_img, width=24).pack(side="left", padx=(6, 2))
        else:
            ctk.CTkLabel(row, text=icon, font=(F_BODY, 12), width=24).pack(side="left", padx=(6, 2))

        ctk.CTkLabel(row, text=msg, font=F_LOG,
                      text_color=color, anchor="w", wraplength=480).pack(side="left")

        self._log_rows.append(row)
        self.after(50, lambda: self._log_frame._parent_canvas.yview_moveto(1))


    def _clear_log(self):
        for row in self._log_rows:
            row.destroy()
        self._log_rows.clear()

    # ── System Tray ───────────────────────────
    def _init_tray(self):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d   = ImageDraw.Draw(img)
        for r in range(28, 0, -1):
            ratio = r / 28
            rc = int(59  + (6   - 59)  * (1 - ratio))
            gc = int(130 + (182 - 130) * (1 - ratio))
            bc = int(246 + (212 - 246) * (1 - ratio))
            d.ellipse([32-r, 32-r, 32+r, 32+r], fill=(rc, gc, bc, 255))
        d.text((21, 16), "A", fill="white")
        menu = pystray.Menu(
            pystray.MenuItem("Show", self._show_window, default=True),
            pystray.MenuItem("Stop Agent", lambda _: self._stop_agent()),
            pystray.MenuItem("Quit", lambda _: self._quit_app()),
        )
        self._tray = pystray.Icon("nat-auto-agent", img, "NAT-AUTO Agent", menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _on_close_to_tray(self):
        self.withdraw()

    def _show_window(self):
        self.after(0, self.deiconify)

    def _quit_app(self):
        self._stop_agent()
        if HAS_TRAY:
            self._tray.stop()
        self.destroy()


# ─────────────────────────────────────────────
#   Entry Point
# ─────────────────────────────────────────────
if __name__ == "__main__":
    app = AgentApp()
    app.mainloop()
