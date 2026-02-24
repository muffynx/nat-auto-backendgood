import eventlet
eventlet.monkey_patch()

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room
from pymongo import MongoClient
from bson.objectid import ObjectId
import datetime as dt
import certifi
import traceback
import io
import os
from datetime import datetime, timezone, timedelta
import secrets  # สำหรับ gen key

import env
from dotenv import load_dotenv
load_dotenv()

from converter import ConfigConverter

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

thai_tz = timezone(timedelta(hours=7))
agent_connections = {}
# DATABASE
MONGO_URI = env.get_env_variable('PYTHON_MONGODB_URI')

client = None
db = None
users_col = None

try:
    client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
    db = client['net_automation']
    users_col = db['users']
    print("✅ Connected to MongoDB Atlas")
except Exception as e:
    print(f"❌ MongoDB Connection Error: {e}")

# ────────────────────────────────────────────────
#             HELPER FUNCTIONS
# ────────────────────────────────────────────────

def serialize_doc(doc: dict) -> dict:
    """Convert MongoDB doc to JSON-serializable dict (ObjectId → str, datetime → ISO string)"""
    result = {}
    for k, v in doc.items():
        if isinstance(v, ObjectId):
            result[k] = str(v)
        elif isinstance(v, (dt.datetime, datetime)):
            result[k] = v.isoformat()
        else:
            result[k] = v
    return result

def get_backup_command(device_type):
    # แปลงเป็นตัวพิมพ์เล็กกันพลาด
    dtype = device_type.lower()
    
    if "cisco" in dtype or "aruba_osswitch" in dtype or "aruba_aoscx" in dtype or "aruba" in dtype:
        return "show running-config"
        
    elif "juniper" in dtype:
        return "show configuration"
        
    elif "hp_comware" in dtype or "huawei" in dtype or "hp" in dtype or "comware" in dtype:
        return "display current-configuration"
        
    elif "fortinet" in dtype:
        return "show full-configuration"
        
    else:
        return "show running-config" # Default


def parse_vlan_range(vlan_str):
    vlans = []
    try:
        parts = vlan_str.split(',')
        for part in parts:
            part = part.strip()
            if '-' in part:
                start, end = map(int, part.split('-'))
                vlans.extend(range(start, end + 1))
            elif 'to' in part:
                start, end = map(int, part.split('to'))
                vlans.extend(range(start, end + 1))
            else:
                vlans.append(int(part))
    except:
        pass
    return sorted(list(set(vlans)))


def generate_bulk_vlan_config(device_type, vlan_str, vlan_name_prefix, svi_id, ip_address, subnet_mask):
    configs = []
    dtype = device_type.lower()
    vlan_list = parse_vlan_range(vlan_str)

    # VLAN creation
    if "cisco" in dtype or "aruba" in dtype:
        for vid in vlan_list:
            configs.append(f"vlan {vid}")
            if vlan_name_prefix:
                configs.append(f"name {vlan_name_prefix}_{vid}")
            configs.append("exit")
    elif "hp_comware" in dtype or "huawei" in dtype:
        batch_str = " ".join(map(str, vlan_list))
        configs.append(f"vlan batch {batch_str}")
        if vlan_name_prefix:
            for vid in vlan_list:
                configs.append(f"vlan {vid}")
                configs.append(f"name {vlan_name_prefix}_{vid}")
                configs.append("quit")

    # SVI / L3 interface
    if svi_id and ip_address and subnet_mask:
        if "cisco" in dtype or "aruba" in dtype:
            configs.append(f"interface vlan {svi_id}")
            configs.append(f"ip address {ip_address} {subnet_mask}")
            configs.append("no shutdown")
            configs.append("exit")
        elif "hp_comware" in dtype or "huawei" in dtype:
            configs.append(f"interface Vlan-interface {svi_id}")
            configs.append(f"ip address {ip_address} {subnet_mask}")
            configs.append("quit")

    return configs

def get_device_driver(device):
    return {
        'device_type': device['device_type'],
        'host': device['ip_address'],
        'username': device['username'],
        'password': device['password'],
        'secret': device.get('secret', ''),
        'port': int(device.get('port', 22)),
        'global_delay_factor': 0.5,
        'fast_cli': True,           # ✅ เปิดโหมด Fast (ช่วยได้เยอะใน Cisco/Aruba)
        'banner_timeout': 10,       # เผื่อ Banner ยาว
        'auth_timeout': 10,         # เผื่อ Authentication ช้า
    }


# ────────────────────────────────────────────────
#             SOCKET.IO - รับผลจาก Agent
# ────────────────────────────────────────────────
@socketio.on('task_result')
def handle_task_result(data):
    task_type = data.get('type')
    status = data.get('status')
    hostname = data.get('hostname')
    owner = data.get('owner')

    print(f"[TASK RESULT] {task_type} - {hostname} - {status} (owner: {owner})")

    # 1. กรณีเป็นงาน Backup
    if task_type == 'backup':
        backup_doc = {
            'device_id': data.get('device_id'),
            'hostname': hostname,
            'owner': owner,
            'config_data': data.get('output', ''),
            'status': status,
            'timestamp': dt.datetime.now(thai_tz),
        }
        
        if status == 'Failed':
            backup_doc['config_data'] = data.get('output', str(data.get('error', 'Unknown error')))

        # บันทึกลง DB
        db.backups.insert_one(backup_doc)

        # ส่งต่อไป frontend
        emit('backup_update', {
            'device_id': data.get('device_id'),
            'hostname': hostname,
            'status': status,
            'percent': 100 if status in ['Success', 'Failed'] else data.get('percent', 50),
            'msg': 'Backup Complete' if status == 'Success' else 'Backup Failed',
            'output': data.get('output', '')
        }, room=owner)

    # 2. กรณีเป็นงาน Command / Config ธรรมดา ให้ส่งเข้า Terminal
    elif task_type in ['run_command', 'push_config']:
        socketio.emit('terminal_update', data)
        
    # ✅ 3. กรณีเป็น Batch Config ให้แยกส่ง Event ไปหาหน้าต่าง Batch โดยเฉพาะ!
    elif task_type == 'batch_config':
        socketio.emit('batch_config_result', data)


# ────────────────────────────────────────────────
#             API ROUTES
# ────────────────────────────────────────────────

@socketio.on('register_agent')
def handle_register_agent(data):
    agent_key = data.get('agent_key')
    if not agent_key:
        emit('agent_auth_failed', {'message': 'No agent_key provided'})
        return

    key_doc = db.agent_keys.find_one({'key': agent_key, 'is_active': True})
    if not key_doc:
        emit('agent_auth_failed', {'message': 'Invalid or inactive agent key'})
        return

    user = key_doc['user']
    db.agent_keys.update_one(
        {'key': agent_key},
        {'$set': {'last_used': dt.datetime.now(thai_tz)}}
    )

    # Join room ชื่อ user เพื่อรับ task เฉพาะ
    join_room(user)
    #บันทึกว่า User นี้มี Agent ออนไลน์อยู่
    agent_connections[request.sid] = user
    emit('agent_auth_success', {'user': user})
    print(f"Agent authenticated and joined room: {user}")

@socketio.on('disconnect')
def handle_disconnect():
    if request.sid in agent_connections:
        user = agent_connections.pop(request.sid)
        print(f"⚠️ Agent disconnected for user: {user}")

@app.route('/api/download-agent', methods=['GET'])
def download_agent():
    """Serve the NetPilot Agent exe for download."""
    exe_path = os.path.join(os.path.dirname(__file__), 'dist', 'agent_gui.exe')
    if not os.path.exists(exe_path):
        return jsonify({'error': 'Agent executable not found on server'}), 404
    return send_file(
        exe_path,
        mimetype='application/octet-stream',
        as_attachment=True,
        download_name='NetPilot-Agent.exe'
    )

@app.route('/api/batch_config', methods=['POST'])
def api_batch_config():
    current_user = request.headers.get('X-Username')
    if not current_user:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    devices = data.get('devices', [])          # list of device dicts
    commands = data.get('commands', [])
    profile_id = data.get('profile_id')  # รับจาก frontend

    if not devices or not commands:
        return jsonify({'error': 'Missing devices or commands'}), 400

    # ส่งงานไป agent พร้อม profile_id
    socketio.emit('execute_task', {
        'type': 'batch_config',
        'devices': devices,
        'commands': commands,
        'owner': current_user,
        'profile_id': profile_id
    })

    return jsonify({
        'status': 'dispatched',
        'message': f'Batch config sent to agents ({len(devices)} devices)'
    })


@app.route('/api/run_backup', methods=['POST'])
def run_backup():
    current_user = request.headers.get('X-Username')
    if not current_user:
        return jsonify({'error': 'Unauthorized'}), 401

    # สมมติ frontend ส่ง profile_id มาด้วย หรือดึงทั้งหมดของ user
    profile_id = request.json.get('profile_id')

    query = {'owner': current_user}
    if profile_id:
        query['profile_id'] = profile_id

    devices = list(db.devices.find(query))

    if not devices:
        return jsonify({'message': 'No devices found'}), 200

    # แปลง ObjectId และ datetime เป็น str ก่อนส่ง
    devices = [serialize_doc(dev) for dev in devices]

    socketio.emit('execute_task', {
        'type': 'batch_backup',
        'devices': devices,
        'owner': current_user,
        'profile_id': profile_id
    })

    return jsonify({
        'status': 'dispatched',
        'total_devices': len(devices),
        'message': 'Batch backup task has been sent to agents'
    })


@app.route('/api/config_vlan_ip', methods=['POST'])
def config_vlan_ip():
    current_user = request.headers.get('X-Username')
    if not current_user:
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    device = data.get('device')  # single device dict
    vlan_range = data.get('vlan_range')
    vlan_name = data.get('vlan_name')
    svi_id = data.get('svi_id')
    ip_address = data.get('ip_address')
    subnet_mask = data.get('subnet_mask')
    profile_id = data.get('profile_id')

    if not device or not vlan_range:
        return jsonify({'error': 'Missing parameters'}), 400

    config_lines = generate_bulk_vlan_config(
        device['device_type'],
        vlan_range,
        vlan_name,
        svi_id,
        ip_address,
        subnet_mask
    )

    # ส่งไป agent พร้อม profile_id
    socketio.emit('execute_task', {
        'type': 'push_config',
        'device': device,
        'commands': config_lines,
        'owner': current_user,
        'profile_id': profile_id
    })

    return jsonify({
        'status': 'dispatched',
        'message': 'VLAN config task sent to agent',
        'preview': config_lines[:10]  # แสดงตัวอย่าง
    })


@app.route('/api/run_single_command', methods=['POST'])
def run_single_command():
    current_user = request.headers.get('X-Username')
    if not current_user:
        return jsonify({'status': 'Failed', 'output': 'Unauthorized'}), 401
    

    if current_user not in agent_connections.values():
        return jsonify({'status': 'Failed', 'output': 'Agent Offline: กรุณาเปิดโปรแกรม NATPILOT Agent ที่คอมพิวเตอร์ของคุณก่อนรันคำสั่ง'}), 400

    data = request.json
    device_id = data.get('device_id')
    command = data.get('command')
    profile_id = data.get('profile_id')

    if not device_id or not command:
        return jsonify({'status': 'Failed', 'output': 'Missing device_id or command'}), 400

    # หา device
    device = db.devices.find_one({'_id': ObjectId(device_id), 'owner': current_user})
    if not device:
        return jsonify({'status': 'Failed', 'output': 'Device not found or access denied'}), 404

    # แปลง _id และ datetime ทุก field เป็น str ก่อนส่งให้ agent
    device = serialize_doc(device)

    # ส่งงานไป agent พร้อม profile_id
    socketio.emit('execute_task', {
        'type': 'run_command',
        'device': device,
        'command': command,
        'owner': current_user,
        'profile_id': profile_id
    })

    return jsonify({
        'status': 'dispatched',
        'message': 'Command execution task sent to agent'
    })


@app.route('/api/devices', methods=['GET'])
def get_devices():
    current_user = request.headers.get('X-Username')
    profile_id = request.args.get('profile_id')
    if not current_user:
        return jsonify([])

    query = {'owner': current_user}
    if profile_id:
        query['profile_id'] = profile_id

    devices = list(db.devices.find(query))
    for dev in devices:
        dev['_id'] = str(dev['_id'])
        dev['command_preview'] = get_backup_command(dev['device_type'])
    return jsonify(devices)


@app.route('/api/backups', methods=['GET'])
def get_backups():
    current_user = request.headers.get('X-Username')
    profile_id = request.args.get('profile_id')
    if not current_user:
        return jsonify([])

    query = {'owner': current_user}

    if profile_id:
        profile_devices = list(db.devices.find(
            {'owner': current_user, 'profile_id': profile_id},
            {'_id': 1}
        ))
        device_ids = [str(d['_id']) for d in profile_devices]
        if device_ids:
            query['device_id'] = {'$in': device_ids}
        else:
            return jsonify([])

    backups = list(db.backups.find(query).sort('timestamp', -1).limit(100))
    for b in backups:
        b['_id'] = str(b['_id'])
        b['device_id'] = str(b.get('device_id', ''))
    return jsonify(backups)


# --- USER MANAGEMENT API ---


# ✅ API: Convert Config
@app.route('/api/convert_config', methods=['POST'])
def convert_config_api():
    current_user = request.headers.get('X-Username')
    if not current_user: return jsonify({'msg': 'Unauthorized'}), 401

    source_type = None
    target_type = None
    log_content = None

    # CASE 1: Excel Upload
    if request.content_type and 'multipart/form-data' in request.content_type:
        source_type = request.form.get('source_type')
        target_type = request.form.get('target_type')
        if 'file' not in request.files: return jsonify({'msg': 'No file'}), 400
        log_content = request.files['file'].read() # bytes

    # CASE 2: Text JSON
    else:
        data = request.json
        source_type = data.get('source_type')
        target_type = data.get('target_type')
        log_content = data.get('log_content') # string

    if not source_type or not target_type or not log_content:
        return jsonify({'status': 'error', 'msg': 'Missing parameters'}), 400

    try:
        # ✅ เรียกใช้ Class (ตอนนี้ __init__ รับ 3 ค่าแล้ว ถูกต้อง)
        converter = ConfigConverter(source_type, target_type, log_content)
        result_config = converter.process()

        return jsonify({'status': 'success', 'output': result_config})

    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'msg': str(e)}), 500


# ✅ API: Export Excel (แก้เพิ่ม Route และ Clean Header)
@app.route('/api/export_excel', methods=['POST'])
def export_excel_api():
    current_user = request.headers.get('X-Username')
    
    log_content = request.json.get('log_content')
    source_type = request.json.get('source_type')
    
    if not log_content: return jsonify({'msg': 'No content'}), 400

    try:
        # 1. Init Converter
        converter = ConfigConverter(source_type, "aruba_cx", log_content)
        
        # 2. ✅ Clean Header ก่อน Parse (สำคัญ! ไม่งั้น Parse ไม่เจอ)
        if isinstance(converter.raw_log, str):
            for header in ["display current-configuration", "show running-config"]:
                if header in converter.raw_log:
                    converter.raw_log = converter.raw_log.split(header, 1)[1]

        # 3. Parse ตาม Source Type
        if source_type == "hp_comware":
            converter._parse_comware()
        elif source_type == "cisco_ios":
            converter._parse_cisco_ios()
        
        # 4. Export
        excel_data = converter.export_to_excel()
        
        return send_file(
            io.BytesIO(excel_data),
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f"network_spec_{converter.data['hostname']}.xlsx"
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({'status': 'error', 'msg': str(e)}), 500



# --- ADMIN USER MANAGEMENT API ---
@app.route('/api/users', methods=['GET'])
def get_users():
    if users_col is None: return jsonify([]), 500
    users = list(users_col.find())
    for u in users:
        u['_id'] = str(u['_id'])
        if 'password' in u: del u['password'] 
    return jsonify(users)

@app.route('/api/users/<id>', methods=['PUT'])
def update_user(id):
    data = request.json
    update_data = {
        'role': data.get('role'),
        'expire_date': data.get('expire_date')
    }
    if data.get('password'):
        update_data['password'] = data['password']

    users_col.update_one({'_id': ObjectId(id)}, {'$set': update_data})
    return jsonify({'msg': '✅ แก้ไขข้อมูลสำเร็จ'})

@app.route('/api/users/<id>', methods=['DELETE'])
def delete_user(id):
    users_col.delete_one({'_id': ObjectId(id)})
    return jsonify({'msg': '🗑️ ลบผู้ใช้งานสำเร็จ'})
# ✅ API: จัดการ PROFILES (Sites)

@app.route('/api/profiles', methods=['GET'])
def get_profiles():
    current_user = request.headers.get('X-Username')
    if not current_user: return jsonify([])
    
    profiles = list(db.profiles.find({'owner': current_user}))
    for p in profiles:
        p['_id'] = str(p['_id'])
    return jsonify(profiles)

@app.route('/api/profiles', methods=['POST'])
def create_profile():
    current_user = request.headers.get('X-Username')
    data = request.json
    
    new_profile = {
        'name': data.get('name'),
        'owner': current_user,
        'created_at': dt.datetime.now()
    }
    result = db.profiles.insert_one(new_profile)
    return jsonify({'msg': 'Profile created', 'id': str(result.inserted_id)})

@app.route('/api/profiles/<id>', methods=['PUT'])
def update_profile(id):
    current_user = request.headers.get('X-Username')
    data = request.json
    db.profiles.update_one(
        {'_id': ObjectId(id), 'owner': current_user}, 
        {'$set': {'name': data.get('name')}}
    )
    return jsonify({'msg': 'Profile updated'})

@app.route('/api/profiles/<id>', methods=['DELETE'])
def delete_profile(id):
    current_user = request.headers.get('X-Username')
    # 1. ลบ Profile
    db.profiles.delete_one({'_id': ObjectId(id), 'owner': current_user})

    # 1. หา Device ทั้งหมดใน Profile นี้ก่อน (เพื่อเอา ID ไปลบ Log)
    devices_in_profile = list(db.devices.find({'profile_id': id, 'owner': current_user}, {'_id': 1}))
    # แปลง ObjectId เป็น String List
    device_ids_to_delete = [str(d['_id']) for d in devices_in_profile]
    if device_ids_to_delete:
        db.backups.delete_many({'device_id': {'$in': device_ids_to_delete}})
    # 2. ลบอุปกรณ์ทั้งหมดใน Profile นั้นด้วย (Clean up)
    db.devices.delete_many({'profile_id': id, 'owner': current_user})


    db.profiles.delete_one({'_id': ObjectId(id), 'owner': current_user})
    return jsonify({'msg': 'Profile deleted'})

@app.route('/api/login', methods=['POST'])
def login():
    try:
        data = request.json
        username = data.get('username')
        password = data.get('password')

        if users_col is None:
            return jsonify({'status': 'error', 'msg': '❌ Database connection failed'}), 500

        user = users_col.find_one({'username': username, 'password': password})
        
        if not user:
            return jsonify({'status': 'error', 'msg': '❌ ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง'}), 401

        # เช็ควันหมดอายุ
        expire_str = user.get('expire_date') 
        if expire_str:
            try:
                # ✅ ใช้ dt.datetime
                expire_date = dt.datetime.strptime(expire_str, '%Y-%m-%d')
                if dt.datetime.now() > expire_date:
                    return jsonify({'status': 'error', 'msg': '⏳ บัญชีของคุณหมดอายุแล้ว กรุณาติดต่อ Admin'}), 403
            except ValueError:
                print("Date format error, skipping check")

        return jsonify({
            'status': 'success', 
            'msg': 'Login Successful',
            'user': {
                'username': user['username'],
                'role': user.get('role', 'user'),
                'expire_date': expire_str
            }
        })
    except Exception as e:
        print("Login Error:", e)
        traceback.print_exc()
        return jsonify({'status': 'error', 'msg': str(e)}), 500

@app.route('/api/admin/create_user', methods=['POST'])
def create_user():
    data = request.json
    if users_col.find_one({'username': data['username']}):
        return jsonify({'msg': 'User already exists'}), 400
        
    users_col.insert_one({
        'username': data['username'],
        'password': data['password'], 
        'expire_date': data['expire_date'],
        'role': data.get('role', 'user'),
        'created_at': dt.datetime.now() # ✅ ใช้ dt
    })
    return jsonify({'msg': '✅ User created successfully'})


# ✅ API สำหรับแก้ไขอุปกรณ์ (Update Device)
@app.route('/api/devices/<id>', methods=['PUT'])
def update_device(id):
    current_user = request.headers.get('X-Username')
    data = request.json
    
    # เตรียมข้อมูลที่จะแก้
    update_data = {
        'hostname': data['hostname'],
        'ip_address': data['ip_address'],
        'device_type': data['device_type'],
        'username': data['username'],
        'port': int(data.get('port', 22))
    }
    
    # ถ้ามีการกรอก Password ใหม่มา ให้แก้ด้วย (ถ้าส่งค่าว่างมา ไม่ต้องแก้)
    if data.get('password'):
        update_data['password'] = data['password']
    if data.get('secret'):
        update_data['secret'] = data['secret']

    # สั่ง Update โดยต้องเช็คว่าเป็นของ Owner คนนี้จริงๆ
    result = db.devices.update_one(
        {'_id': ObjectId(id), 'owner': current_user},
        {'$set': update_data}
    )
    
    if result.matched_count > 0:
        return jsonify({'msg': 'Device updated successfully'})
    else:
        return jsonify({'msg': 'Device not found or permission denied'}), 404


# --- API ROUTES (ส่วนสำคัญที่ต้องกรอง User) ---


@app.route('/api/devices', methods=['POST'])
def add_device():
    data = request.json
    current_user = request.headers.get('X-Username')
    
    # ต้องส่ง profile_id มาด้วย
    if not data.get('profile_id'):
        return jsonify({'msg': 'Profile ID required'}), 400

    data['owner'] = current_user 
    data['created_at'] = dt.datetime.now()
    
    db.devices.insert_one(data)
    return jsonify({'msg': 'Device added successfully'})

@app.route('/api/devices/<id>', methods=['DELETE'])
def delete_device(id):
    current_user = request.headers.get('X-Username')
    # ✅ ลบเฉพาะถ้า User เป็นเจ้าของ
    result = db.devices.delete_one({'_id': ObjectId(id), 'owner': current_user})
    if result.deleted_count > 0:
        return jsonify({'msg': 'Device deleted'})
    return jsonify({'msg': 'Device not found or permission denied'}), 404









######## API: Generate Agent Key สำหรับ profile นี้
######## API: Generate Agent Key สำหรับ profile นี้



# API: Generate Agent Key สำหรับ user นี้ (gen แค่ครั้งเดียวก็พอใช้ได้ตลอด)
@app.route('/api/generate_agent_key', methods=['POST'])
def generate_agent_key():
    current_user = request.headers.get('X-Username')
    if not current_user:
        return jsonify({'error': 'Unauthorized'}), 401

    existing = db.agent_keys.find_one({'user': current_user, 'is_active': True})
    if existing:
        return jsonify({
            'status': 'exists',
            'agent_key': existing['key'],
            'message': 'คุณมี Agent Key อยู่แล้ว'
        })

    key = secrets.token_hex(32)

    db.agent_keys.insert_one({
        'key': key,
        'user': current_user,
        'created_at': dt.datetime.now(thai_tz),
        'last_used': None,
        'is_active': True
    })

    return jsonify({
        'status': 'success',
        'agent_key': key,
        'message': 'คัดลอก Agent Key นี้ไปตั้งค่าใน .env ของ agent แล้ว restart agent'
    })


# API: แสดงรายการ Agent Key ทั้งหมดของ user นี้ (optional)
@app.route('/api/agent_keys', methods=['GET'])
def list_agent_keys():
    current_user = request.headers.get('X-Username')
    if not current_user:
        return jsonify([])

    keys = list(db.agent_keys.find({
        'user': current_user,
        'is_active': True
    }, {
        'key': 1,
        'created_at': 1,
        'last_used': 1,
        '_id': 0
    }))

    return jsonify(keys)


# API: Revoke (ลบ/ยกเลิก) Agent Key
@app.route('/api/agent_keys/<key>', methods=['DELETE'])
def revoke_agent_key(key):
    current_user = request.headers.get('X-Username')
    if not current_user:
        return jsonify({'error': 'Unauthorized'}), 401

    result = db.agent_keys.delete_one({
        'key': key,
        'user': current_user
    })

    if result.deleted_count > 0:
        return jsonify({'msg': 'Agent Key ถูกยกเลิกเรียบร้อยแล้ว'})
    else:
        return jsonify({'msg': 'ไม่พบ Agent Key หรือไม่มีสิทธิ์'}), 404
if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, allow_unsafe_werkzeug=True)