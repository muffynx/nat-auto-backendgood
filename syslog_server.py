import socket
import re
from datetime import datetime
from pymongo import MongoClient

# ตั้งค่า Database
client = MongoClient('mongodb://localhost:27017/')
db = client['network_automation_db']
collection = db['syslogs']  # เก็บ Log แยกไว้ที่นี่

# ตั้งค่า UDP Server
UDP_IP = "0.0.0.0" # ฟังทุก IP ในเครื่อง
UDP_PORT = 514     # Port มาตรฐาน Syslog

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((UDP_IP, UDP_PORT))

print(f"📡 Syslog Server Listening on port {UDP_PORT}...")

def parse_cisco_log(message):
    """
    ตัวอย่างการแกะ Log ของ Cisco
    Format: <PRI>Seq: Timestamp: %LOG-TYPE: Message
    """
    # 1. จับคนแอบแก้ Config (Configured from console by...)
    if "SYS-5-CONFIG_I" in message:
        # ตัวอย่าง: Configured from console by admin on vty0 (192.168.1.55)
        user_match = re.search(r'by (\w+) on', message)
        ip_match = re.search(r'\(([\d\.]+)\)', message)
        return {
            "type": "CONFIG_CHANGE",
            "user": user_match.group(1) if user_match else "unknown",
            "src_ip": ip_match.group(1) if ip_match else "console",
            "raw": message
        }

    # 2. จับคำสั่งที่พิมพ์ (ต้องเปิด archive log config ก่อน)
    elif "PARSER-5-CFGLOG_LOGGEDCMD" in message:
        # ตัวอย่าง: User:admin  logged command:interface Vlan99
        user_match = re.search(r'User:(\w+)', message)
        cmd_match = re.search(r'logged command:(.+)', message)
        return {
            "type": "COMMAND_EXEC",
            "user": user_match.group(1) if user_match else "unknown",
            "command": cmd_match.group(1).strip() if cmd_match else "",
            "raw": message
        }
    
    return None

while True:
    try:
        data, addr = sock.recvfrom(1024) # รับข้อมูล (Buffer size 1024)
        message = data.decode('utf-8').strip()
        device_ip = addr[0]

        # กรองเอาเฉพาะ Log ที่เราสนใจ
        parsed_data = parse_cisco_log(message)

        if parsed_data:
            log_entry = {
                "device_ip": device_ip,
                "timestamp": datetime.now(),
                "log_type": parsed_data['type'],
                "user": parsed_data.get('user'),
                "details": parsed_data.get('command') or parsed_data.get('raw'),
                "source": "SSH/Console" # ระบุว่ามาจากภายนอก
            }
            
            # บันทึกลง MongoDB
            collection.insert_one(log_entry)
            print(f"🚨 Captured: {device_ip} -> {parsed_data['user']} did {parsed_data.get('command')}")

    except Exception as e:
        print(f"Error: {e}")