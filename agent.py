import socketio
import time
import os
from dotenv import load_dotenv
from netmiko import ConnectHandler
from concurrent.futures import ThreadPoolExecutor, as_completed
import traceback

load_dotenv()

# ────────────────────────────────────────────────
#               CONFIGURATION
# ────────────────────────────────────────────────

VPS_URL = 'http://192.168.74.1:5000'

MAX_WORKERS = 20  # เพิ่มได้ถ้าเครื่องแรง

AGENT_KEY = os.getenv('AGENT_KEY')
if not AGENT_KEY:
    print("ERROR: ต้องตั้งค่า AGENT_KEY ใน .env")
    print("ตัวอย่าง: AGENT_KEY=agk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    exit(1)

sio = socketio.Client(
    reconnection=True,
    reconnection_delay=2,  # ลด delay
    reconnection_attempts=999
)

allowed_user = None
print(f"[START] Agent started with key: {AGENT_KEY[:8]}...")
# ────────────────────────────────────────────────
#               HELPER FUNCTIONS
# ────────────────────────────────────────────────

def get_device_driver(device):
    """ สร้าง dict สำหรับ netmiko ConnectHandler """
    return {
        'device_type': device['device_type'],
        'host': device['ip_address'],
        'username': device['username'],
        'password': device['password'],
        'secret': device.get('secret', ''),
        'port': int(device.get('port', 22)),
        'global_delay_factor': 0.5,
        'fast_cli': True,
        'banner_timeout': 30,
        'auth_timeout': 30,
    }


def get_backup_command(device_type):
    """ คำสั่งดึง running-config ตาม vendor """
    dtype = device_type.lower()
    if "cisco" in dtype or "aruba" in dtype:
        return "show running-config"
    elif "hp" in dtype or "comware" in dtype or "huawei" in dtype:
        return "display current-configuration"
    elif "juniper" in dtype:
        return "show configuration"
    elif "fortinet" in dtype:
        return "show full-configuration"
    return "show running-config"


# ────────────────────────────────────────────────
#               TASK FUNCTIONS
# ────────────────────────────────────────────────

def task_backup(device):
    try:
        driver = get_device_driver(device)
        net_connect = ConnectHandler(**driver)
        cmd = get_backup_command(device['device_type'])
        output = net_connect.send_command(cmd, read_timeout=90)
        net_connect.disconnect()
        return {'status': 'Success', 'output': output}
    except Exception as e:
        err = str(e)
        traceback.print_exc()
        return {'status': 'Failed', 'output': err}


def task_push_config(device, commands):
    try:
        driver = get_device_driver(device)
        net_connect = ConnectHandler(**driver)
        if device.get('secret'):
            net_connect.enable()
        output = net_connect.send_config_set(commands)
        save_cmd = None
        dtype = device['device_type'].lower()
        if "cisco" in dtype or "aruba" in dtype:
            save_cmd = "write memory"
        elif "hp" in dtype or "comware" in dtype or "huawei" in dtype:
            save_cmd = "save force"
        if save_cmd:
            output += "\n" + net_connect.send_command(save_cmd, read_timeout=30)
        net_connect.disconnect()
        return {'status': 'Success', 'output': output}
    except Exception as e:
        err = str(e)
        traceback.print_exc()
        return {'status': 'Failed', 'output': err}


def task_run_command(device, command):
    try:
        print(f"[{device.get('hostname','unknown')}] Executing: {command}")
        driver = get_device_driver(device)
        net_connect = ConnectHandler(**driver)
        output = net_connect.send_command(command, read_timeout=120)
        net_connect.disconnect()
        return {'status': 'Success', 'output': output}
    except Exception as e:
        err = str(e)
        traceback.print_exc()
        return {'status': 'Failed', 'output': err}


# ────────────────────────────────────────────────
#               SOCKET.IO EVENT HANDLERS
# ────────────────────────────────────────────────

@sio.event
def connect():
    print(f"🚀 Connected to server → {VPS_URL}")
    sio.emit('register_agent', {'agent_key': AGENT_KEY})
    print(f"   Registered with agent key: {AGENT_KEY[:8]}...")


@sio.event
def disconnect():
    print("⚠️ Disconnected from server")


@sio.on('agent_auth_success')
def on_agent_auth_success(payload):
    global allowed_user
    user = payload.get('user')
    if user:
        allowed_user = user
        sio.enter_room(user)  # Join room เพื่อรับ task เฉพาะ
        print(f"[AUTH SUCCESS] Authorized for user: {user}")
        print("   Joined room:", user)
        print("   Ready to receive tasks")
    else:
        print("[AUTH WARNING] No user assigned")


@sio.on('agent_auth_failed')
def on_agent_auth_failed(payload):
    msg = payload.get('message', 'Unknown error')
    print(f"[AUTH FAILED] {msg}")
    # Reconnect จะพยายามใหม่เอง


@sio.on('execute_task')
def on_execute_task(payload):
    if allowed_user is None:
        print("[SKIP] Not authorized yet")
        return

    task_owner = payload.get('owner')
    if task_owner != allowed_user:
        print(f"[SKIP] Owner mismatch: {task_owner} != {allowed_user}")
        return

    task_type = payload.get('type')
    print(f"📦 Executing {task_type} (owner: {task_owner})")

    owner = payload.get('owner')  # สำหรับ log ใน task_result

    # ── 1. BACKUP เดี่ยว ──────────────────────────────────────
    if task_type == 'backup':
        device = payload.get('device')
        if not device:
            return

        sio.emit('task_result', {
            'type': 'backup',
            'status': 'Running',
            'percent': 10,
            'msg': 'Connecting...',
            'device_id': device.get('_id'),
            'hostname': device.get('hostname')
        })

        result = task_backup(device)

        sio.emit('task_result', {
            'type': 'backup',
            'status': result['status'],
            'output': result['output'],
            'percent': 100,
            'msg': 'Backup Finished' if result['status'] == 'Success' else 'Backup Failed',
            'device_id': device.get('_id'),
            'hostname': device.get('hostname'),
            'owner': owner
        })

    # ── 2. BATCH BACKUP ───────────────────────────────────────
    elif task_type == 'batch_backup':
        devices = payload.get('devices', [])
        print(f"💾 Batch backup → {len(devices)} devices")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_dev = {executor.submit(task_backup, dev): dev for dev in devices}
            for future in as_completed(future_to_dev):
                dev = future_to_dev[future]
                try:
                    result = future.result()
                    sio.emit('task_result', {
                        'type': 'backup',
                        'status': result['status'],
                        'output': result['output'],
                        'device_id': dev.get('_id'),
                        'hostname': dev.get('hostname'),
                        'owner': owner
                    })
                except Exception as exc:
                    sio.emit('task_result', {
                        'type': 'backup',
                        'status': 'Failed',
                        'output': str(exc),
                        'device_id': dev.get('_id'),
                        'hostname': dev.get('hostname'),
                        'owner': owner
                    })

    # ── 3. PUSH CONFIG เดี่ยว ─────────────────────────────────
    elif task_type == 'push_config':
        device = payload.get('device')
        commands = payload.get('commands', [])

        if not device or not commands:
            return

        result = task_push_config(device, commands)

        sio.emit('task_result', {
            'type': 'push_config',
            'status': result['status'],
            'output': result['output'],
            'hostname': device.get('hostname'),
            'owner': owner
        })

    # ── 4. BATCH CONFIG ───────────────────────────────────────
    elif task_type == 'batch_config':
        devices = payload.get('devices', [])
        commands = payload.get('commands', [])

        if not devices or not commands:
            print("Missing devices or commands in batch_config")
            return

        print(f"⚙️ Batch config → {len(devices)} devices")

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            future_to_dev = {executor.submit(task_push_config, dev, commands): dev for dev in devices}
            for future in as_completed(future_to_dev):
                dev = future_to_dev[future]
                try:
                    result = future.result()
                    sio.emit('task_result', {
                        'type': 'batch_config',
                        'status': result['status'],
                        'output': result['output'],
                        'hostname': dev.get('hostname'),
                        'owner': owner
                    })
                except Exception as exc:
                    sio.emit('task_result', {
                        'type': 'batch_config',
                        'status': 'Failed',
                        'output': str(exc),
                        'hostname': dev.get('hostname'),
                        'owner': owner
                    })

    # ── 5. RUN COMMAND เดี่ยว ─────────────────────────────────
    elif task_type == 'run_command':
        device = payload.get('device')
        command = payload.get('command')

        if not device or not command:
            return

        result = task_run_command(device, command)

        sio.emit('task_result', {
            'type': 'run_command',
            'status': result['status'],
            'output': result['output'],
            'hostname': device.get('hostname'),
            'owner': owner
        })


# ────────────────────────────────────────────────
#                   MAIN LOOP
# ────────────────────────────────────────────────

if __name__ == '__main__':
    while True:
        try:
            if not sio.connected:
                print(f"Connecting to {VPS_URL} ...")
                sio.connect(VPS_URL, wait_timeout=5)
            sio.wait()
        except Exception as e:
            print(f"Connection error: {e}")
            time.sleep(2)  # ลด delay