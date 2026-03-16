"""
agent.py — NAT-AUTO Private Network Agent
รันบน server ที่อยู่ใน private network

Flow:
  Brain (app.py) emit 'execute_task' → Agent รับ → SSH → emit 'task_result' กลับ
"""

import socketio
import datetime
from netmiko import ConnectHandler

# ===== CONFIG =====
BACKEND       = "http://localhost:5000"   # URL ของ Brain (Public VPS)
AGENT_SECRET  = "agent-secret-key"       # ต้องตรงกับ AGENT_SECRET ใน .env ของ Brain


# ===== SOCKET.IO CLIENT =====
sio = socketio.Client(reconnection=True, reconnection_attempts=0, reconnection_delay=3)


# ===== HELPER: เลือกคำสั่ง backup ตาม vendor =====
def get_backup_command(device_type: str) -> str:
    dtype = device_type.lower()
    if 'cisco' in dtype or 'aruba_osswitch' in dtype or 'aruba_aoscx' in dtype:
        return 'show running-config'
    elif 'juniper' in dtype:
        return 'show configuration'
    elif 'hp_comware' in dtype or 'huawei' in dtype:
        return 'display current-configuration'
    elif 'fortinet' in dtype:
        return 'show full-configuration'
    return 'show running-config'


# ===== HELPER: สร้าง Netmiko driver dict =====
def get_driver(device: dict) -> dict:
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
        'auth_timeout':   15,
    }


# ===== JOB HANDLERS =====

def handle_backup(device: dict) -> str:
    driver = get_driver(device)
    net_connect = ConnectHandler(**driver)
    cmd = get_backup_command(device['device_type'])
    output = net_connect.send_command(cmd, read_timeout=90)
    net_connect.disconnect()
    return output


def handle_run_command(device: dict, command: str) -> str:
    driver = get_driver(device)
    net_connect = ConnectHandler(**driver)
    output = net_connect.send_command(command, read_timeout=30)
    net_connect.disconnect()
    return output


def handle_push_config(device: dict, config_lines: list) -> str:
    driver = get_driver(device)
    net_connect = ConnectHandler(**driver)
    if device.get('secret'):
        net_connect.enable()
    output = net_connect.send_config_set(config_lines)
    dtype = device['device_type'].lower()
    if 'cisco' in dtype or 'aruba' in dtype:
        output += '\n' + net_connect.send_command('write memory')
    elif 'hp_comware' in dtype or 'huawei' in dtype:
        output += '\n' + net_connect.send_command('save force')
    net_connect.disconnect()
    return output


def process_job(data: dict) -> tuple:
    """รัน job แล้วคืน (status, output)"""
    device  = data.get('device', {})
    payload = data.get('payload', {})
    jtype   = data.get('type', '')

    try:
        if jtype == 'backup':
            output = handle_backup(device)
        elif jtype == 'run_command':
            output = handle_run_command(device, payload.get('command', ''))
        elif jtype == 'push_config':
            output = handle_push_config(device, payload.get('config_lines', []))
        else:
            return 'failed', f'Unknown job type: {jtype}'
        return 'done', output
    except Exception as e:
        return 'failed', str(e)


# ===== SOCKET.IO EVENTS =====

@sio.event(namespace='/agent')
def connect():
    print(f"[{datetime.datetime.now()}] ✅ Connected to Brain at {BACKEND}")

@sio.event(namespace='/agent')
def disconnect():
    print(f"[{datetime.datetime.now()}] ⚠️  Disconnected from Brain — reconnecting...")

@sio.on('execute_task', namespace='/agent')
def on_execute_task(data):
    """Brain ส่งงานมา — รัน SSH แล้วส่งผลกลับทันที"""
    job_id = data.get('job_id')
    jtype  = data.get('type', '?')
    host   = data.get('device', {}).get('ip_address', '?')
    owner  = data.get('owner', '')
    device = data.get('device', {})

    print(f"[{datetime.datetime.now()}] ▶ Job {job_id} | type={jtype} | host={host}")

    status, output = process_job(data)

    # ส่งผลกลับ Brain ทันที
    sio.emit('task_result', {
        'job_id': job_id,
        'type':   jtype,
        'status': status,
        'output': output,
        'owner':  owner,
        'device': device,
    }, namespace='/agent')

    print(f"[{datetime.datetime.now()}] {'✅' if status == 'done' else '❌'} Job {job_id} → {status}")


# ===== MAIN =====

if __name__ == '__main__':
    print(f"[{datetime.datetime.now()}] 🤖 Agent starting — connecting to {BACKEND}")
    sio.connect(
        BACKEND,
        namespaces=['/agent'],
        auth={'secret': AGENT_SECRET},
        transports=['websocket'],
        socketio_path='/socket.io',
    )
    sio.wait()
