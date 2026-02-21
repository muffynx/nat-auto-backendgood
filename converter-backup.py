import re
from unicodedata import name


class ConfigConverter:
    def __init__(self, source_type, target_type, log_content):
        self.source = source_type
        self.target = target_type
        self.raw_log = log_content

        self.data = {
            "hostname": "Switch",
            "vlans": {},        # vid -> { name, ip, mask }
            "routes": [],      # static routes
            "interfaces": {}   # port -> role data
        }

# ================= MAIN =================
    def process(self):
        if not self.raw_log:
            return "Error: Empty log content"

        # (Logic ตัด Header เดิม...)
        if "display current-configuration" in self.raw_log:
            self.raw_log = self.raw_log.split("display current-configuration", 1)[1]
        
        # เพิ่ม Logic สำหรับ Cisco ตรงนี้ครับ
        if "show running-config" in self.raw_log:
             self.raw_log = self.raw_log.split("show running-config", 1)[1]

        # -------------------------------------------------------
        # กรณี 1: HPE Comware -> Aruba
        if self.source == "hp_comware" and self.target in ("aruba_cx", "aruba_os_switch"):
            self._parse_comware()
            return self._generate_aruba_cx()
        
        # -------------------------------------------------------
        # กรณี 2: Cisco IOS (2960) -> Aruba (เพิ่มใหม่)
        elif self.source == "cisco_ios" and self.target in ("aruba_cx", "aruba_os_switch"):
            self._parse_cisco_ios()  # <--- เรียกฟังก์ชันใหม่
            return self._generate_aruba_cx() # ใช้ตัว Gen เดิมได้เลยเพราะโครงสร้าง data เหมือนกัน
        # -------------------------------------------------------

        return f"Error: Conversion {self.source} -> {self.target} not supported"

    # ================= PARSER =================
    def _parse_comware(self):

        # ---------- Hostname ----------
        m = re.search(r"sysname\s+(\S+)", self.raw_log)
        if m:
            self.data["hostname"] = m.group(1)





        # ---------- VLAN Definition ----------
        vlan_blocks = re.findall(
            r"^vlan (\d+)(.*?)(?=^vlan |\n#)",
            self.raw_log,
            re.DOTALL | re.MULTILINE
        )

        for vid, content in vlan_blocks:
            vid = int(vid)
            self.data["vlans"][vid] = {
                "name": f"VLAN_{vid}",
                "ip": "",
                "mask": ""
            }

            d = re.search(r"description\s+(.+)", content)
            if d:
                self.data["vlans"][vid]["name"] = d.group(1).strip()





        # ---------- Interface Parsing ----------
        interfaces = re.findall(
            r"^interface ([^\n]+)\n(.*?)(?=\n#)",
            self.raw_log,
            re.DOTALL | re.MULTILINE
        )



        for raw_name, cfg in interfaces:

            # Skip SVI
            if "Vlan-interface" in raw_name:
                continue

            port = self._map_interface_name(raw_name)
            if not port:
                continue

            self.data["interfaces"][port] = {
                "description": "",
                "mode": None,            # access / trunk
                "access_vlan": None,
                "native_vlan": None,
                "allowed_vlans": set()
            }

            # ----- Access Port -----
            m = re.search(r"port access vlan (\d+)", cfg)
            if m:
                self.data["interfaces"][port]["mode"] = "access"
                self.data["interfaces"][port]["access_vlan"] = int(m.group(1))
                continue

            # ----- Trunk Port -----
            if "port link-type trunk" in cfg:
                self.data["interfaces"][port]["mode"] = "trunk"

                m = re.search(r"port trunk pvid vlan (\d+)", cfg)
                self.data["interfaces"][port]["native_vlan"] = int(m.group(1)) if m else 1

                m = re.search(r"port trunk permit vlan (.+)", cfg)
                if m:
                    vids = {int(v) for v in re.findall(r"\d+", m.group(1))}
                    self.data["interfaces"][port]["allowed_vlans"] = vids

             # ---- Description -----       
        d = re.search(r"description\s+(.+)", cfg)
        if d:
            self.data["interfaces"][port]["description"] = d.group(1).strip()


        # ---------- SVI ----------
        svis = re.findall(
            r"^interface Vlan-interface(\d+)\n(.*?)(?=\n#)",
            self.raw_log,
            re.DOTALL | re.MULTILINE
        )
        # IPv6 Support
        m6 = re.search(r"ipv6 address ([0-9a-fA-F:]+)/(\d+)", cfg)
        if m6:
            self.data["vlans"][vid]["ipv6"] = f"{m6.group(1)}/{m6.group(2)}"
        self.data["vlans"][vid] = {
    "name": f"VLAN_{vid}",
    "ip": "",
    "mask": "",
    "ipv6": ""
}
        
        
        for dest, nh in re.findall(
    r"ipv6 route-static (::) \d+ ([0-9a-fA-F:]+)",
    self.raw_log
):
            self.data["routes"].append({
                "ipv6": True,
                "dest": dest,
                "next_hop": nh
            })


        for vid, cfg in svis:
            vid = int(vid)
            m = re.search(
                r"ip address (\d+\.\d+\.\d+\.\d+) (\d+\.\d+\.\d+\.\d+)",
                cfg
            )
            if m:
                self.data["vlans"].setdefault(
                    vid, {"name": f"VLAN_{vid}", "ip": "", "mask": ""}
                )
                self.data["vlans"][vid]["ip"] = m.group(1)
                self.data["vlans"][vid]["mask"] = m.group(2)

        # ---------- Routes ----------
        for d, m, nh in re.findall(
            r"ip route-static (\S+) (\S+) (\S+)",
            self.raw_log
        ):
            self.data["routes"].append({
                "dest": d,
                "mask": m,
                "next_hop": nh
            })



    # ================= PARSER (CISCO) =================
    def _parse_cisco_ios(self):
        # ---------- Hostname ----------
        m = re.search(r"^hostname\s+(\S+)", self.raw_log, re.MULTILINE)
        if m:
            self.data["hostname"] = m.group(1)

        # ---------- VLAN Definition ----------
        # Cisco: vlan 10 \n name SALES
        vlan_blocks = re.findall(
            r"^vlan (\d+)\n(.*?)(?=^vlan |^interface |^!)", 
            self.raw_log, 
            re.DOTALL | re.MULTILINE
        )

        for vid, content in vlan_blocks:
            vid = int(vid)
            self.data["vlans"][vid] = {
                "name": f"VLAN_{vid}", "ip": "", "mask": ""
            }
            # หาชื่อ VLAN
            d = re.search(r"name\s+(\S+)", content)
            if d:
                self.data["vlans"][vid]["name"] = d.group(1).strip()

        # ---------- Interface Parsing (Physical) ----------
        interfaces = re.findall(
            r"^interface ([^\n]+)\n(.*?)(?=^interface |^!)", 
            self.raw_log, 
            re.DOTALL | re.MULTILINE
        )

        for raw_name, cfg in interfaces:
            # ข้าม VLAN Interface (SVI) ไปก่อน
            if raw_name.lower().startswith("vlan"):
                continue

            port = self._map_interface_name(raw_name)
            if not port: continue

            # สร้างโครงสร้าง Data รอไว้
            self.data["interfaces"][port] = {
                "mode": None, 
                "access_vlan": 1, # Cisco default access is VLAN 1
                "native_vlan": 1, 
                "allowed_vlans": set()
            }

            # ----- Mode Check (Access / Trunk) -----
            mode_match = re.search(r"switchport mode (access|trunk)", cfg)
            
            # กรณี Cisco 2960 บางทีถ้าไม่มี config mode มันคือ Access
            mode = mode_match.group(1) if mode_match else "access"
            
            # ถ้า config มีคำว่า trunk allowed หรือ trunk native ให้ถือเป็น trunk แน่นอน
            if "switchport trunk" in cfg:
                mode = "trunk"

            self.data["interfaces"][port]["mode"] = mode

            # ----- Access Port Config -----
            if mode == "access":
                m = re.search(r"switchport access vlan (\d+)", cfg)
                if m:
                    self.data["interfaces"][port]["access_vlan"] = int(m.group(1))

            # ----- Trunk Port Config -----
            elif mode == "trunk":
                # Native VLAN
                m = re.search(r"switchport trunk native vlan (\d+)", cfg)
                if m:
                    self.data["interfaces"][port]["native_vlan"] = int(m.group(1))
                
                # Allowed VLANs
                m = re.search(r"switchport trunk allowed vlan ([\d,-]+)", cfg) # จับ 10,20-30
                if m:
                    vlan_str = m.group(1)
                    allowed_set = set()
                    # ฟังก์ชั่นแตก string (เช่น "10,20-22")
                    for part in vlan_str.split(','):
                        if '-' in part:
                            start, end = map(int, part.split('-'))
                            allowed_set.update(range(start, end + 1))
                        else:
                            allowed_set.add(int(part))
                    self.data["interfaces"][port]["allowed_vlans"] = allowed_set

        # ---------- SVI (Interface Vlan) ----------
        svis = re.findall(
            r"^interface Vlan(\d+)\n(.*?)(?=^interface |^!)", 
            self.raw_log, 
            re.DOTALL | re.MULTILINE
        )

        for vid, cfg in svis:
            vid = int(vid)
            # ip address 192.168.1.1 255.255.255.0
            m = re.search(r"ip address (\d+\.\d+\.\d+\.\d+) (\d+\.\d+\.\d+\.\d+)", cfg)
            
            if m:
                # ถ้ายังไม่มี VLAN นี้ใน list (บางที cisco ไม่ประกาศ vlan แต่มี int vlan)
                self.data["vlans"].setdefault(vid, {"name": f"VLAN_{vid}", "ip": "", "mask": ""})
                
                self.data["vlans"][vid]["ip"] = m.group(1)
                self.data["vlans"][vid]["mask"] = m.group(2)

        # ---------- Static Routes ----------
        # ip route 0.0.0.0 0.0.0.0 192.168.1.254
        routes = re.findall(r"^ip route (\S+) (\S+) (\S+)", self.raw_log, re.MULTILINE)
        for d, m, nh in routes:
            self.data["routes"].append({
                "dest": d, "mask": m, "next_hop": nh
            })







    # ================= HELPERS =================
    def _map_interface_name(self, name):
        """
        HPE Comware -> Aruba CX
        """
        name = name.strip()


        
        # ================= HPE Comware =================
        # GigabitEthernet1/0/10  -> 1/1/10
        m = re.match(r"GigabitEthernet(\d+)/\d+/(\d+)", name)
        if m:
            member = m.group(1)
            port = m.group(2)
            return f"{member}/1/{port}"

        # Ten-GigabitEthernet1/1/1 -> 1/1/1
        m = re.match(r"Ten-GigabitEthernet(\d+)/\d+/(\d+)", name)
        if m:
            member = m.group(1)
            port = m.group(2)
            return f"{member}/1/{port}"




        # ---------- LAG ----------
        if name.startswith("Bridge-Aggregation"):
            lag = name.replace("Bridge-Aggregation", "")
            return f"Lag{lag}"

        if name.startswith("Port-channel"):
            lag = name.replace("Port-channel", "")
            return f"Lag{lag}"
        



        # ---------- Cisco ----------
        m = re.match(r"(FastEthernet|GigabitEthernet|TenGigabitEthernet)\d+/(\d+)", name)
        if m:
            port = m.group(2)
            return f"1/1/{port}"
        



        return None

    # ================= GENERATOR =================
    def _generate_aruba_cx(self):
        lines = [
            f'hostname "{self.data["hostname"]}"',
            "ip routing",
            "!"
        ]

        # ---------- VLAN ----------
        for vid in sorted(self.data["vlans"]):
            v = self.data["vlans"][vid]
            lines.append(f"vlan {vid}")
            lines.append(f'   name "{v["name"]}"')

            if v["ip"]:
                lines.append(f"   ip address {v['ip']} {v['mask']}")

            lines.append("   exit")
            lines.append("!")

        # ---------- INTERFACES ----------
        for port in sorted(self.data["interfaces"]):
            iface = self.data["interfaces"][port]

            # ❗ skip interface ที่ไม่มี role
            if iface["mode"] is None:
                continue

            lines.append(f"interface {port}")

            if iface["description"]:
                lines.append(f"   description {iface['description']}")

            if iface["mode"] == "access":
                lines.append(f"   vlan access {iface['access_vlan']}")

            elif iface["mode"] == "trunk":
                native = iface["native_vlan"] or 1
                allowed = sorted(v for v in iface["allowed_vlans"] if v != native)

                lines.append(f"   vlan trunk native {native}")
                if allowed:
                    lines.append(f"   vlan trunk allowed {','.join(map(str, allowed))}")

            lines.append("   exit")
            lines.append("!")


        # ---------- DEFAULT ROUTE (เลือกเส้นแรก) ----------
        if self.data["routes"]:
            r = self.data["routes"][0]
            lines.append(
                f"ip route {r['dest']} {r['mask']} {r['next_hop']}"
            )

        return "\n".join(lines)
