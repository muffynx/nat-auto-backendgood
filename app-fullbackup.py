import env
from dotenv import load_dotenv
load_dotenv()
import eventlet
eventlet.monkey_patch()
from flask import Flask, request, jsonify
from flask_cors import CORS
from pymongo import MongoClient
from bson.objectid import ObjectId
from netmiko import ConnectHandler
import datetime as dt  # ✅ ใช้ dt เพื่อป้องกัน Error 500
import certifi
import concurrent.futures 
import traceback 
from flask import send_file # ✅ สำหรับส่งไฟล์ดาวน์โหลด
from converter import ConfigConverter # ✅ Import Class ใหม่
import io
from flask_socketio import SocketIO, emit 
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from datetime import timezone, timedelta

thai_tz = timezone(timedelta(hours=7))
datetime.now(thai_tz)


app = Flask(__name__)
CORS(app) 
import os




socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# --- DATABASE CONFIG ---
# ⚠️ อย่าลืมเช็ค Password ใน MONGO_URI อีกทีนะครับ
MONGO_URI = env.get_env_variable('PYTHON_MONGODB_URI')


users_col = None
db = None

try:
    client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
    db = client['net_automation']
    users_col = db['users'] 
    print("✅ Connected to MongoDB Atlas")
except Exception as e:
    print(f"❌ MongoDB Connection Error: {e}")

# api to save logs


@socketio.on('start_backup_realtime')
def handle_realtime_backup(data):
    device_id = data.get('device_id')
    username = data.get('username') # รับชื่อคนทำมาด้วย
    
    # 1. หาอุปกรณ์
    device = db.devices.find_one({'_id': ObjectId(device_id)})
    if not device:
        emit('backup_update', {'status': 'error', 'msg': 'Device not found', 'percent': 0})
        return

    try:
        # [Step 1] เริ่มเชื่อมต่อ (10%)
        emit('backup_update', {'status': 'running', 'msg': f'Connecting to {device["hostname"]}...', 'percent': 10})
        eventlet.sleep(0) # Yield ให้ Socket ทำงาน
        
        driver = get_device_driver(device)
        net_connect = ConnectHandler(**driver)
        
        # [Step 2] Login สำเร็จ (40%)
        emit('backup_update', {'status': 'running', 'msg': 'Logged in! Fetching config...', 'percent': 40})
        eventlet.sleep(0)

        # [Step 3] ส่งคำสั่ง (70%)
        cmd = get_backup_command(device['device_type'])
        output = net_connect.send_command(cmd, read_timeout=60)
        net_connect.disconnect()
        
        emit('backup_update', {'status': 'running', 'msg': 'Saving to database...', 'percent': 80})
        eventlet.sleep(0)

        # [Step 4] บันทึกลง DB
        db.backups.insert_one({
            'device_id': str(device['_id']),
            'hostname': device['hostname'],
            'owner': username,
            'config_data': output,
            'timestamp': dt.datetime.now(),
            'status': 'Success'
        })

        # [Step 5] เสร็จสิ้น (100%)
        emit('backup_update', {'status': 'success', 'msg': 'Backup Complete!', 'percent': 100, 'output': output})

    except Exception as e:
        # ถ้าพัง ส่ง Error กลับไป
        error_msg = str(e)
        emit('backup_update', {'status': 'error', 'msg': f'Error: {error_msg}', 'percent': 100})
        
        # บันทึก Error Log
        db.backups.insert_one({
            'device_id': str(device['_id']),
            'hostname': device['hostname'],
            'owner': username,
            'config_data': error_msg,
            'timestamp': dt.datetime.now(),
            'status': 'Failed'
        })

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

# --- AUTH ROUTES ---

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


@app.route('/api/run_single_command', methods=['POST'])
def run_single_command():
    current_user = request.headers.get('X-Username')
    data = request.json
    device_id = data.get('device_id')
    command = data.get('command') # รับคำสั่งที่ User พิมพ์มา เช่น "show ip route"
    
    # 1. หาอุปกรณ์
    device = db.devices.find_one({'_id': ObjectId(device_id), 'owner': current_user})
    if not device:
        return jsonify({'status': 'Failed', 'output': 'Device not found'}), 404

    try:
        # 2. ต่ออุปกรณ์
        driver = get_device_driver(device)
        net_connect = ConnectHandler(**driver)
        
        # 3. ส่งคำสั่งที่ User ขอมา
        # (เพิ่ม read_timeout เผื่อคำสั่งพวก ping มันนาน)
        output = net_connect.send_command(command, read_timeout=10) 
        net_connect.disconnect()
        
        # 4. ส่งผลลัพธ์กลับไปหน้าเว็บทันที (ไม่บันทึกลง DB)
        return jsonify({'status': 'Success', 'output': output})

    except Exception as e:
        return jsonify({'status': 'Failed', 'output': str(e)})


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


# --- HELPER FUNCTIONS ---


def generate_vlan_config(device_type, vlan_id, vlan_name, ip_address, subnet_mask):
    configs = []
    dtype = device_type.lower()
    
    # 1. กลุ่ม Cisco IOS / Aruba Switch / Aruba CX
    if "cisco" in dtype or "aruba_osswitch" in dtype or "aruba_aoscx" in dtype:
        # สร้าง VLAN
        configs.append(f"vlan {vlan_id}")
        if vlan_name: configs.append(f"name {vlan_name}")
        configs.append("exit")
        
        # ใส่ IP ที่ Interface VLAN
        if ip_address and subnet_mask:
            configs.append(f"interface vlan {vlan_id}")
            configs.append(f"ip address {ip_address} {subnet_mask}")
            configs.append("no shutdown")
            configs.append("exit")

    # 2. กลุ่ม HPE Comware / Huawei (คำสั่งจะต่างออกไป)
    elif "hp_comware" in dtype or "huawei" in dtype:
        # สร้าง VLAN
        configs.append(f"vlan {vlan_id}")
        if vlan_name: configs.append(f"name {vlan_name}")
        configs.append("quit")
        
        # ใส่ IP (ต้องใช้คำว่า Vlan-interface)
        if ip_address and subnet_mask:
            configs.append(f"interface Vlan-interface {vlan_id}")
            configs.append(f"ip address {ip_address} {subnet_mask}")
            configs.append("quit")

    return configs

# ✅ API: รับค่าจากหน้าเว็บมายิง Config# ✅ 1. Helper: แปลง String "10, 20-25" ให้กลายเป็น List [10, 20, 21, 22, 23, 24, 25]
def parse_vlan_range(vlan_str):
    vlans = []
    try:
        parts = vlan_str.split(',')
        for part in parts:
            part = part.strip()
            if '-' in part:
                start, end = map(int, part.split('-'))
                vlans.extend(range(start, end + 1))
            elif 'to' in part: # เผื่อคนชินคำสั่ง HPE "10 to 20"
                start, end = map(int, part.split('to'))
                vlans.extend(range(start, end + 1))
            else:
                vlans.append(int(part))
    except:
        pass # ถ้ากรอกมั่วๆ มาก็ข้ามไป
    return sorted(list(set(vlans))) # เรียงเลข + ตัดตัวซ้ำ

# ✅ 2. Helper: สร้าง Config ชุดใหญ่ (รองรับ Bulk Creation)
def generate_bulk_vlan_config(device_type, vlan_str, vlan_name_prefix, svi_id, ip_address, subnet_mask):
    configs = []
    dtype = device_type.lower()
    vlan_list = parse_vlan_range(vlan_str)
    
    # --- STEP 1: VLAN CREATION (LAYER 2) ---
    
    if "cisco" in dtype or "aruba_osswitch" in dtype or "aruba_aoscx" in dtype:
        # Cisco Loop สร้างทีละตัว
        for vid in vlan_list:
            configs.append(f"vlan {vid}")
            if vlan_name_prefix:
                configs.append(f"name {vlan_name_prefix}_{vid}")
            configs.append("exit")
            
    elif "hp_comware" in dtype or "huawei" in dtype:
        # HPE/Huawei ใช้ Batch Command ทีเดียวจบ (เร็วมาก)
        # แปลง list [10, 11, 12] เป็น string "10 to 12" หรือ "10 11 12"
        # เพื่อความง่าย ส่งเป็น space separated ไปเลย
        batch_str = " ".join(map(str, vlan_list))
        configs.append(f"vlan batch {batch_str}")
        
        # วนลูปใส่ชื่อ (ถ้าต้องการ)
        if vlan_name_prefix:
            for vid in vlan_list:
                configs.append(f"vlan {vid}")
                configs.append(f"name {vlan_name_prefix}_{vid}")
                configs.append("quit")

    # --- STEP 2: LAYER 3 INTERFACE (OPTIONAL) ---
    
    if svi_id and ip_address and subnet_mask:
        if "cisco" in dtype or "aruba_osswitch" in dtype or "aruba_aoscx" in dtype:
            configs.append(f"interface vlan {svi_id}")
            configs.append(f"ip address {ip_address} {subnet_mask}")
            configs.append("no shutdown")
            configs.append("exit")
            
        elif "hp_comware" in dtype or "huawei" in dtype:
            configs.append(f"interface Vlan-interface {svi_id}")
            configs.append(f"ip address {ip_address} {subnet_mask}")
            configs.append("quit")

    return configs

# ✅ 3. API: Config VLAN (แก้จากอันเดิม)
@app.route('/api/config_vlan_ip', methods=['POST'])
def config_vlan_ip():
    current_user = request.headers.get('X-Username')
    data = request.json
    device_id = data.get('device_id')
    
    # รับค่า
    vlan_range = data.get('vlan_range') # ex: "10, 20-30"
    vlan_name = data.get('vlan_name')   # ex: "STAFF" -> STAFF_10
    
    # ส่วน L3 (แยกออกมา เพื่อความยืดหยุ่น)
    svi_id = data.get('svi_id')         # VLAN ไหนที่จะใส่ IP
    ip_address = data.get('ip_address')
    subnet_mask = data.get('subnet_mask')

    device = db.devices.find_one({'_id': ObjectId(device_id), 'owner': current_user})
    if not device: return jsonify({'status': 'Failed', 'msg': 'Device not found'}), 404

    try:
        driver = get_device_driver(device)
        net_connect = ConnectHandler(**driver)
        
        # เรียกใช้ฟังก์ชันใหม่
        config_set = generate_bulk_vlan_config(
            device['device_type'], 
            vlan_range, 
            vlan_name, 
            svi_id, 
            ip_address, 
            subnet_mask
        )
        
        output = net_connect.send_config_set(config_set)
        
        # Save
        if "cisco" in device['device_type'] or "aruba" in device['device_type']:
            output += "\n" + net_connect.send_command("write memory")
        elif "hp_comware" in device['device_type'] or "huawei" in device['device_type']:
            output += "\n" + net_connect.send_command("save force") 
            
        net_connect.disconnect()
        
        return jsonify({'status': 'Success', 'output': output})

    except Exception as e:
        return jsonify({'status': 'Failed', 'output': str(e)}), 500

def parse_vlan_range(vlan_str):
    vlans = []
    try:
        parts = vlan_str.split(',')
        for part in parts:
            part = part.strip()
            if '-' in part:
                start, end = map(int, part.split('-'))
                vlans.extend(range(start, end + 1))
            elif 'to' in part: # เผื่อคนชินคำสั่ง HPE "10 to 20"
                start, end = map(int, part.split('to'))
                vlans.extend(range(start, end + 1))
            else:
                vlans.append(int(part))
    except:
        pass # ถ้ากรอกมั่วๆ มาก็ข้ามไป
    return sorted(list(set(vlans))) # เรียงเลข + ตัดตัวซ้ำ





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
def task_backup(device):
    try:
        driver = get_device_driver(device)
        net_connect = ConnectHandler(**driver)
        
        # ดึงคำสั่งจากฟังก์ชันกลาง (ไม่ต้องเขียน If-Else ซ้ำ)
        cmd = get_backup_command(device['device_type'])
        
        # ส่งคำสั่ง (ครั้งเดียวพอ)
        output = net_connect.send_command(cmd, read_timeout=90)
        net_connect.disconnect()
        
        # บันทึกลง DB
        db.backups.insert_one({
            'device_id': str(device['_id']),
            'hostname': device['hostname'],
            'owner': device.get('owner'), 
            'config_data': output,
            'timestamp': dt.datetime.now(),
            'status': 'Success'
        })
        return {'host': device['hostname'], 'status': 'Success'}
        
    except Exception as e:
        # ถ้าพัง ให้บันทึก Error
        db.backups.insert_one({
            'device_id': str(device['_id']),
            'hostname': device['hostname'],
            'owner': device.get('owner'),
            'config_data': str(e),
            'timestamp': dt.datetime.now(),
            'status': 'Failed'
        })
        return {'host': device['hostname'], 'status': 'Failed', 'error': str(e)}

def task_send_command(device, command):
    try:
        driver = get_device_driver(device)
        net_connect = ConnectHandler(**driver)
        output = net_connect.send_command(command)
        net_connect.disconnect()
        return {'host': device['hostname'], 'status': 'Success', 'output': output}
    except Exception as e:
        return {'host': device['hostname'], 'status': 'Failed', 'error': str(e)}
# ---------------------------------------------------------
# 1. Worker Function: ฟังก์ชันสำหรับ Config อุปกรณ์ 1 ตัว
# ---------------------------------------------------------
def task_push_config(device, config_lines):
    try:
        driver = get_device_driver(device)
        net_connect = ConnectHandler(**driver)
        output = net_connect.send_config_set(config_lines)
        if "cisco" in device['device_type']:
            net_connect.send_command("write memory")
        net_connect.disconnect()
        return {'host': device['hostname'], 'status': 'Success', 'log': output}
    except Exception as e:
        return {'host': device['hostname'], 'status': 'Failed', 'error': str(e)}
    
# ---------------------------------------------------------
# 2. API Route: รับคำสั่ง Batch Config
# ---------------------------------------------------------

@app.route('/api/batch_config', methods=['POST'])
def api_batch_config():
    data = request.json
    
    # รับข้อมูลจาก Frontend
    target_devices = data.get('devices', []) # List ของอุปกรณ์ที่ติ๊กเลือกมา
    config_commands = data.get('commands', []) # List ของคำสั่ง (เช่น ['vlan 10', 'name SALES'])
    
    if not target_devices or not config_commands:
        return jsonify({"error": "Missing devices or commands"}), 400

    results = []
    
    # 🔥 เริ่มทำงานแบบ ThreadPool (Parallel)
    # max_workers=10 คือทำพร้อมกันสูงสุด 10 ตัว (ปรับได้ตามความแรงเครื่อง Server)
    with ThreadPoolExecutor(max_workers=10) as executor:
        # สร้าง List ของงาน (Future objects)
        future_to_device = {
            executor.submit(task_push_config, device, config_commands): device 
            for device in target_devices
        }
        
        # รอรับผลลัพธ์เมื่องานเสร็จ (as_completed)
        for future in as_completed(future_to_device):
            device = future_to_device[future]
            try:
                data = future.result()
                results.append(data)
            except Exception as exc:
                # กันเหนียวเผื่อ Worker ตาย
                results.append({
                    "host": device.get('host'),
                    "status": "failed",
                    "log": f"Worker Exception: {exc}"
                })

    # ส่งผลลัพธ์กลับไปให้ Frontend แสดงผล
    return jsonify({
        "summary": {
            "total": len(target_devices),
            "success": len([r for r in results if r['status'] == 'success']),
            "failed": len([r for r in results if r['status'] == 'failed'])
        },
        "details": results
    })





def get_backup_command(device_type):
    # แปลงเป็นตัวพิมพ์เล็กกันพลาด
    dtype = device_type.lower()
    
    if "cisco" in dtype or "aruba_osswitch" in dtype or "aruba_aoscx" in dtype:
        return "show running-config"
        
    elif "juniper" in dtype:
        return "show configuration"
        
    elif "hp_comware" in dtype or "huawei" in dtype:
        return "display current-configuration"
        
    elif "fortinet" in dtype:
        return "show full-configuration"
        
    else:
        return "show running-config" # Default





# --- API ROUTES (ส่วนสำคัญที่ต้องกรอง User) ---

@app.route('/api/devices', methods=['GET'])
def get_devices():
    current_user = request.headers.get('X-Username')
    profile_id = request.args.get('profile_id') # รับค่าจาก Query Param
    
    if not current_user or not profile_id: return jsonify([])
    
    # กรองตาม Owner และ Profile ID
    devices = list(db.devices.find({'owner': current_user, 'profile_id': profile_id}))
    for dev in devices:
        dev['_id'] = str(dev['_id'])
        # คำนวณ command preview เหมือนเดิม
        dev['command_preview'] = get_backup_command(dev['device_type'])
        
    return jsonify(devices)

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

@app.route('/api/run_backup_single/<id>', methods=['POST'])
def run_backup_single(id):
    current_user = request.headers.get('X-Username')
    # ✅ หาอุปกรณ์เฉพาะของ User นี้
    device = db.devices.find_one({'_id': ObjectId(id), 'owner': current_user})
    
    if not device:
        return jsonify({'status': 'Failed', 'msg': 'Device not found'}), 404

    result = task_backup(device)
    return jsonify(result)

@app.route('/api/run_backup', methods=['POST'])
def run_backup():
    current_user = request.headers.get('X-Username')
    # ✅ ดึงเฉพาะอุปกรณ์ของ User นี้ไป Backup
    devices = list(db.devices.find({'owner': current_user}))
    results = []
    
    if not devices:
        return jsonify({'msg': 'No devices found for this user'})

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(task_backup, dev): dev for dev in devices}
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
            
    return jsonify(results)

@app.route('/api/run_command', methods=['POST'])
def run_command():
    current_user = request.headers.get('X-Username')
    data = request.json
    command = data.get('command')
    
    # ✅ กรองอุปกรณ์
    devices = list(db.devices.find({'owner': current_user}))
    results = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(task_send_command, dev, command): dev for dev in devices}
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    return jsonify(results)

@app.route('/api/push_config', methods=['POST'])
def push_config():
    current_user = request.headers.get('X-Username')
    data = request.json
    config_lines = data.get('configs')
    
    # ✅ กรองอุปกรณ์
    devices = list(db.devices.find({'owner': current_user}))
    results = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(task_push_config, dev, config_lines): dev for dev in devices}
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    return jsonify(results)

@app.route('/api/backups', methods=['GET'])
def get_backups():
    current_user = request.headers.get('X-Username')
    profile_id = request.args.get('profile_id') # ✅ รับค่า profile_id จาก Frontend
    if not current_user: return jsonify([])
    query = {'owner': current_user}

    if profile_id:
        # 1. ไปหา ID ของอุปกรณ์ทั้งหมดใน Profile นี้มาก่อน
        profile_devices = list(db.devices.find({'owner': current_user, 'profile_id': profile_id}, {'_id': 1}))
        
        # 2. แปลง ObjectId เป็น String (เพราะใน Logs เราเก็บ device_id เป็น String)
        target_device_ids = [str(d['_id']) for d in profile_devices]
        
        # 3. สั่งให้หา Log เฉพาะที่มี device_id อยู่ในรายการนี้
        query['device_id'] = {'$in': target_device_ids}


        # ถ้า Profile นี้ไม่มี Device เลย -> ก็ต้องไม่คืนค่า Log อะไรเลยกลับไป
        if not profile_devices:
            return jsonify([]) 
            
        target_device_ids = [str(d['_id']) for d in profile_devices]
        query['device_id'] = {'$in': target_device_ids}

    backups = list(db.backups.find(query).sort('timestamp', -1).limit(50))
    
    for b in backups:
        b['_id'] = str(b['_id'])
            
    # ✅ ดึงเฉพาะ Log ของ User นี้
    logs = list(db.backups.find({'owner': current_user}).sort('timestamp', -1).limit(50))
    for log in logs:
        log['_id'] = str(log['_id'])
        log['device_id'] = str(log.get('device_id', ''))
    return jsonify(logs)

if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
