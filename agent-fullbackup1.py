import socketio
import time
from netmiko import ConnectHandler
from concurrent.futures import ThreadPoolExecutor, as_completed
import traceback

# ────────────────────────────────────────────────
#               CONFIGURATION
# ────────────────────────────────────────────────

# เปลี่ยนเป็น IP จริงของ server (VPS / Flask + Socket.IO)
# ตัวอย่าง: 'http://your-public-ip:5000' หรือ domain
VPS_URL = 'http://192.168.74.1:5000'   # ← แก้ตรงนี้ให้เป็นค่าจริง !

SITE_ID = 'HQ'           # ใช้แยก agent แต่ละ site ถ้ามีหลายที่
MAX_WORKERS = 10         # จำนวน thread พร้อมกันสูงสุด (ปรับตาม spec เครื่อง)

sio = socketio.Client(
    reconnection=True,
    reconnection_delay=5,
    reconnection_attempts=0   # 0 = พยายาม reconnect ไม่จำกัด
)

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
#               TASK FUNCTIONS (ทำงานจริง)
# ────────────────────────────────────────────────

def task_backup(device):
    """ Backup config ของอุปกรณ์ 1 ตัว """
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
    """ Push configuration lines ไปยังอุปกรณ์ 1 ตัว """
    try:
        driver = get_device_driver(device)
        net_connect = ConnectHandler(**driver)

        # เข้าโหมด enable ถ้ามี secret
        if device.get('secret'):
            net_connect.enable()

        output = net_connect.send_config_set(commands)

        # พยายาม save config อัตโนมัติตาม vendor
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
    """ รันคำสั่งใด ๆ 1 คำสั่ง (show, ping, traceroute ฯลฯ) """
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
    sio.emit('register_agent', {'site_id': SITE_ID})


@sio.event
def disconnect():
    print("⚠️ Disconnected from server")


@sio.on('execute_task')
def on_execute_task(payload):
    task_type = payload.get('type')
    print(f"\n📦 Received task: {task_type}")

    owner = payload.get('owner')

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
            future_to_dev = {
                executor.submit(task_backup, dev): dev
                for dev in devices
            }

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
            future_to_dev = {
                executor.submit(task_push_config, dev, commands): dev
                for dev in devices
            }

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

    # ── 5. RUN COMMAND เดี่ยว (ถ้าต้องการใช้ในอนาคต) ─────────
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
                sio.connect(VPS_URL, wait_timeout=10)
            sio.wait()
        except Exception as e:
            print(f"Connection error: {e}")
            print("Reconnecting in 5 seconds...")
            time.sleep(5)