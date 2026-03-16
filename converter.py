import re
import pandas as pd
import io



class ConfigConverter:
    def __init__(self, source_type, target_type, input_data):
        self.source = source_type
        self.target = target_type
        
        # input_data รับได้ทั้ง string (Log) และ bytes (Excel)
        self.input_data = input_data
        
        # ถ้าส่งมาเป็น Text ให้ map เข้า raw_log ด้วย (เพื่อให้ Parser เดิมทำงานได้)
        self.raw_log = input_data if isinstance(input_data, str) else None

        self.data = {
            "hostname": "Switch",
            "banner": "",
            "vlans": {},        # vid -> { name, ip, mask, ipv6 }
            "routes": [],       # static routes
            "interfaces": {},   # port -> role data
            "dhcp_pools": [],   # list of { name, network, mask, gateway, dns }
            "dhcp_relays": [],  # list of { interface, helper_ips: [] }
            "ntp_servers": [],  # list of strings (IPs)
            "aaa_commands": [], # list of plain aaa strings
            "radius_servers": [], # list of { ip, key }
            "tacacs_servers": [],  # list of { ip, key }
            "snmp_commands": []   # list of bare snmp commands
        }

    # ================= MAIN =================
    def process(self, sections=None):
        if self.source == "excel":
            try:
                self._parse_excel()
            except Exception as e:
                return f"Error parsing Excel: {str(e)}"
# 2. Parse Text Log (Logic เดิม)
        elif isinstance(self.input_data, str): 
            self.raw_log = self.input_data
            if not self.raw_log: return "Error: Empty log"
            
            # Clean Headers
            for header in ["display current-configuration", "show running-config"]:
                if header in self.raw_log:
                    self.raw_log = self.raw_log.split(header, 1)[1]

            if self.source == "hp_comware":
                self._parse_comware()
            elif self.source == "cisco_ios":
                self._parse_cisco_ios()
            else:
                return f"Error: Source {self.source} not supported"
        else:
            return "Error: Invalid input format"

        # ถ้าส่ง sections มา ให้กรองข้อมูลก่อน Generate
        if sections:
            self.filter_sections(sections)

# 3. Generate Config
        if self.target in ("aruba_cx", "aruba_os_switch"):
            return self._generate_aruba_cx_ready_to_paste()
        elif self.target == "cisco_ios":
            return "Error: Cisco Generator coming soon..." # เผื่ออนาคต
        elif self.target == "hp_comware":
            return "Error: Comware Generator coming soon..." # เผื่ออนาคต

        return f"Error: Target {self.target} not supported"

    # ================= SECTION FILTER =================
    def filter_sections(self, sections: list):
        """Clear data for sections NOT in the requested list."""
        # String fields (clear to "")
        STRING_SECTIONS = {
            "banner": "banner",
        }
        # Dict/list fields
        COLLECTION_SECTIONS = {
            "interfaces":  "interfaces",
            "vlans":       "vlans",
            "routes":      "routes",
            "dhcp_pool":   "dhcp_pools",
            "dhcp_relay":  "dhcp_relays",
            "ntp":         "ntp_servers",
            "aaa":         "aaa_commands",
            "radius":      "radius_servers",
            "tacacs":      "tacacs_servers",
            "snmp":        "snmp_commands",
        }
        for section_key, data_key in STRING_SECTIONS.items():
            if section_key not in sections:
                self.data[data_key] = ""
        for section_key, data_key in COLLECTION_SECTIONS.items():
            if section_key not in sections:
                if data_key in self.data:
                    empty = {} if isinstance(self.data[data_key], dict) else []
                    self.data[data_key] = empty


# ================= EXPORTER (Log -> Excel) 🆕 =================
    def export_to_excel(self):
        # สร้าง Buffer ใน Memory (ไม่ต้องเขียนไฟล์ลง Disk)
        output = io.BytesIO()
        writer = pd.ExcelWriter(output, engine='xlsxwriter')

        # 1. Sheet: Global
        df_global = pd.DataFrame([
            {'Parameter': 'Hostname', 'Value': self.data.get('hostname', '')},
            {'Parameter': 'Banner', 'Value': 'Configured' if self.data.get('banner') else 'None'},
            {'Parameter': 'VLAN_Count', 'Value': len(self.data.get('vlans', {}))},
            {'Parameter': 'Interface_Count', 'Value': len(self.data.get('interfaces', {}))},
            {'Parameter': 'Route_Count', 'Value': len(self.data.get('routes', []))}
        ])

        df_global.to_excel(writer, sheet_name='Global', index=False)

        # 2. Sheet: VLANs
        vlan_list = []
        for vid, v in self.data['vlans'].items():
            vlan_list.append({
                'ID': vid,
                'Name': v['name'],
                'IPv4': v['ip'],
                'Mask': v['mask'],
                'IPv6': v['ipv6']
            })
        pd.DataFrame(vlan_list).to_excel(writer, sheet_name='VLANs', index=False)

        # 3. Sheet: Interfaces
        iface_list = []
        # เรียงพอร์ตให้สวยงาม
        sorted_ports = sorted(self.data['interfaces'].keys(), key=self._iface_sort_key)
        
        for port in sorted_ports:
            i = self.data['interfaces'][port]
            
            # แปลง Set เป็น String "10,20,30"
            allowed_str = ""
            if i['allowed_vlans']:
                allowed_str = ",".join(map(str, sorted(list(i['allowed_vlans']))))

            iface_list.append({
                'Port': port,
                'Description': i['description'],
                'Role': i['role'] if i['role'] else '',
                'Access_VLAN': i['access_vlan'] if i['role'] == 'access' else '',
                'Native_VLAN': i['native_vlan'] if i['role'] == 'trunk' else '',
                'Allowed_VLANs': allowed_str,
                'LAG_ID': i['lag_id'] if i['lag_id'] else '',
                'Shutdown': 'Yes' if i['shutdown'] else 'No'
            })
        pd.DataFrame(iface_list).to_excel(writer, sheet_name='Interfaces', index=False)

        # 4. Sheet: Routes
        route_list = []
        for r in self.data.get('routes', []):
            route_list.append({
                'Destination': r['dest'],
                'Mask': r['mask'],
                'Next_Hop': r['next_hop']
            })
        pd.DataFrame(route_list).to_excel(writer, sheet_name='Routes', index=False)

        # 5. Sheet: DHCP Pools 🆕
        pool_list = []
        for p in self.data.get('dhcp_pools', []):
            pool_list.append({
                'Name': p['name'],
                'Network': p['network'],
                'Mask': p['mask'],
                'Gateway': p['gateway'],
                'DNS': p['dns']
            })
        pd.DataFrame(pool_list).to_excel(writer, sheet_name='DHCP Pools', index=False)

        # 6. Sheet: DHCP Relays 🆕
        relay_list = []
        for r in self.data.get('dhcp_relays', []):
            relay_list.append({
                'Interface': r['interface'],
                'Helper_IPs': ", ".join(r['helper_ips'])
            })
        pd.DataFrame(relay_list).to_excel(writer, sheet_name='DHCP Relays', index=False)

        workbook  = writer.book
        worksheet = writer.sheets['Interfaces']

        # ------------------------
        # Formats
        # ------------------------
        header_fmt = workbook.add_format({
            'bold': True,
            'align': 'center',
            'valign': 'middle',
            'border': 1,
            'bg_color': '#D9E1F2'
        })

        access_fmt = workbook.add_format({
            'bg_color': '#E2EFDA',  # เขียวอ่อน
            'border': 1
        })

        trunk_fmt = workbook.add_format({
            'bg_color': '#FFF2CC',  # เหลืองอ่อน
            'border': 1
        })

        default_fmt = workbook.add_format({
            'border': 1
        })

        shutdown_fmt = workbook.add_format({
            'bg_color': '#F8CBAD',  # แดงอ่อน
            'border': 1
        })

        center_fmt = workbook.add_format({
            'align': 'center',
            'border': 1
        })

        wrap_fmt = workbook.add_format({
            'text_wrap': True,
            'border': 1
        })

        # ------------------------
        # Header formatting
        # ------------------------
        for col_num, col_name in enumerate(pd.DataFrame(iface_list).columns):
            worksheet.write(0, col_num, col_name, header_fmt)

        # ------------------------
        # Column width
        # ------------------------
        worksheet.set_column('A:A', 12)   # Port
        worksheet.set_column('B:B', 22)   # Description
        worksheet.set_column('C:C', 10)   # Role
        worksheet.set_column('D:F', 14)   # VLANs
        worksheet.set_column('G:G', 25)   # Allowed VLANs
        worksheet.set_column('H:H', 10)   # LAG
        worksheet.set_column('I:I', 10)   # Shutdown

        # ------------------------
        # Freeze header
        # ------------------------
        worksheet.freeze_panes(1, 0)

        # ------------------------
        # Auto Filter
        # ------------------------
        if iface_list:
            worksheet.autofilter(
                0, 0,
                len(iface_list),
                len(iface_list[0]) - 1
            )

        # ------------------------
        # Row formatting by Role
        # ------------------------
        for row_idx, row in enumerate(iface_list, start=1):
            role = row['Role']
            shutdown = row['Shutdown']

            if shutdown == 'Yes':
                fmt = shutdown_fmt
            elif role == 'access':
                fmt = access_fmt
            elif role == 'trunk':
                fmt = trunk_fmt
            else:
                fmt = default_fmt

            worksheet.set_row(row_idx, None, fmt)


        # Save & Return Bytes
        writer.close()
        output.seek(0)
        return output.read()
    

# ================= PARSER (EXCEL) 🆕 =================
    def _parse_excel(self):
        # อ่านไฟล์ Excel จาก Memory (Bytes)
        # ต้องแน่ใจว่า input_data ถูกส่งมาเป็น bytes (read() จาก file upload)
        xls = pd.ExcelFile(io.BytesIO(self.input_data))

        # 1. Sheet: Global
        if 'Global' in xls.sheet_names:
            df_global = pd.read_excel(xls, 'Global')
            # แปลงเป็น Dict: {'Hostname': 'SW1', 'Banner': '...'}
            global_map = dict(zip(df_global['Parameter'], df_global['Value']))
            
            if 'Hostname' in global_map:
                self.data["hostname"] = str(global_map['Hostname'])
            if 'Banner' in global_map:
                self.data["banner"] = str(global_map['Banner'])

        # 2. Sheet: VLANs
        if 'VLANs' in xls.sheet_names:
            df_vlan = pd.read_excel(xls, 'VLANs').fillna('')
            for _, row in df_vlan.iterrows():
                try:
                    vid = int(row['ID'])
                    self.data["vlans"][vid] = {
                        "name": str(row['Name']),
                        "ip": str(row['IPv4']),
                        "mask": str(row['Mask']),
                        "ipv6": str(row['IPv6'])
                    }
                except:
                    pass

        # 3. Sheet: Interfaces
        if 'Interfaces' in xls.sheet_names:
            df_iface = pd.read_excel(xls, 'Interfaces').fillna('')
            for _, row in df_iface.iterrows():
                port_name = str(row['Port']).strip()
                if not port_name: continue
                
                # แปลงรหัสพอร์ตให้ได้ standard format
                port = self._map_interface_name(port_name)
                if not port:
                    port = port_name # fallback

                iface = self._init_interface_data("")
                iface["description"] = str(row['Description']).strip()
                
                role = str(row['Role']).strip().lower()
                if role in ("access", "trunk", "lag_member"):
                    iface["role"] = role

                if iface["role"] == "access" and row['Access_VLAN']:
                    try: iface["access_vlan"] = int(row['Access_VLAN'])
                    except: pass
                
                if iface["role"] == "trunk" and row['Native_VLAN']:
                    try: iface["native_vlan"] = int(row['Native_VLAN'])
                    except: pass
                
                if row['Allowed_VLANs']:
                    iface["allowed_vlans"] = self._parse_vlan_list(str(row['Allowed_VLANs']))
                
                if row['LAG_ID']:
                    iface["lag_id"] = str(row['LAG_ID'])
                
                if str(row['Shutdown']).strip().lower() == 'yes':
                    iface["shutdown"] = True
                
                # Handle OSPF Cost if exists
                if 'OSPF_Cost' in row and row['OSPF_Cost']:
                    try: iface["ospf_cost"] = int(row['OSPF_Cost'])
                    except: pass
                    
                self.data["interfaces"][port] = iface

        # 4. Sheet: Routes
        if 'Routes' in xls.sheet_names:
            df_routes = pd.read_excel(xls, 'Routes').fillna('')
            for _, row in df_routes.iterrows():
                dest = str(row['Destination']).strip()
                if dest:
                    self.data["routes"].append({
                        "version": "ipv4",
                        "dest": dest,
                        "mask": str(row['Mask']).strip(),
                        "next_hop": str(row['Next_Hop']).strip()
                    })

        # 5. Sheet: DHCP Pools 🆕
        if 'DHCP Pools' in xls.sheet_names:
            df_pools = pd.read_excel(xls, 'DHCP Pools').fillna('')
            for _, row in df_pools.iterrows():
                pool_name = str(row['Name']).strip()
                if pool_name:
                    self.data["dhcp_pools"].append({
                        "name": pool_name,
                        "network": str(row['Network']).strip(),
                        "mask": str(row['Mask']).strip(),
                        "gateway": str(row['Gateway']).strip(),
                        "dns": str(row['DNS']).strip()
                    })

        # 6. Sheet: DHCP Relays 🆕
        if 'DHCP Relays' in xls.sheet_names:
            df_relays = pd.read_excel(xls, 'DHCP Relays').fillna('')
            for _, row in df_relays.iterrows():
                interface = str(row['Interface']).strip()
                if interface:
                    helpers_str = str(row['Helper_IPs']).strip()
                    helpers = [h.strip() for h in helpers_str.split(',')] if helpers_str else []
                    self.data["dhcp_relays"].append({
                        "interface": interface,
                        "helper_ips": helpers
                    })

        # 3. Sheet: Interfaces
        if 'Interfaces' in xls.sheet_names:
            df_int = pd.read_excel(xls, 'Interfaces').fillna('')
            for _, row in df_int.iterrows():
                raw_port = str(row['Port'])
                
                # รองรับ Range เช่น "1/1/1-1/1/24"
                ports = self._expand_port_range(raw_port)
                
                for port in ports:
                    # Map Role
                    role = str(row['Role']).lower().strip()
                    mode = None
                    lag_id = None
                    
                    if role == 'access': mode = 'access'
                    elif role == 'trunk': mode = 'trunk'
                    elif role == 'lag_member': 
                        mode = 'lag_member'
                        if row['LAG_ID']: lag_id = str(int(row['LAG_ID']))

                    # VLANs
                    acc_vlan = int(row['Access_VLAN']) if row['Access_VLAN'] else 1
                    nat_vlan = int(row['Native_VLAN']) if row['Native_VLAN'] else 1
                    
                    # Allowed VLANs (แยกด้วย comma)
                    allowed = set()
                    if row['Allowed_VLANs']:
                        for v in str(row['Allowed_VLANs']).split(','):
                            if v.strip().isdigit(): allowed.add(int(v))

                    shutdown = str(row['Shutdown']).lower() == 'yes'

                    self.data["interfaces"][port] = {
                        "description": str(row['Description']),
                        "role": mode,
                        "access_vlan": acc_vlan,
                        "native_vlan": nat_vlan,
                        "allowed_vlans": allowed,
                        "lag_id": lag_id,
                        "shutdown": shutdown
                    }

        # 4. Sheet: Routes
        if 'Routes' in xls.sheet_names:
            df_route = pd.read_excel(xls, 'Routes').fillna('')
            for _, row in df_route.iterrows():
                self.data["routes"].append({
                    "dest": str(row['Destination']),
                    "mask": str(row['Mask']),
                    "next_hop": str(row['Next_Hop'])
                })

    # Helper: ขยาย Range พอร์ต (1/1/1-1/1/5 -> [1/1/1, 1/1/2...])
    def _expand_port_range(self, port_str):
        if '-' not in port_str: return [port_str]
        
        try:
            start_p, end_p = port_str.split('-')
            # สมมติ format เป็น member/slot/num (เช่น 1/1/1)
            prefix = start_p.rsplit('/', 1)[0] # 1/1
            s_num = int(start_p.rsplit('/', 1)[1]) # 1
            e_num = int(end_p.rsplit('/', 1)[1])   # 5
            
            return [f"{prefix}/{i}" for i in range(s_num, e_num + 1)]
        except:
            return [port_str] # ถ้า format แปลกๆ ให้คืนค่าเดิม
        


    # ================= PARSER: HPE COMWARE =================
    def _parse_comware(self):
        # Normalize line endings (รองรับไฟล์ที่มาจาก Windows \r\n)
        self.raw_log = self.raw_log.replace('\r\n', '\n').replace('\r', '\n')

        m = re.search(r"sysname\s+(\S+)", self.raw_log)
        if m: self.data["hostname"] = m.group(1)

        banner_m = re.search(r"header legal\s+(.)(.*?)\1", self.raw_log, re.DOTALL)
        if banner_m: self.data["banner"] = banner_m.group(2).strip()

        # VLANs
        vlan_blocks = re.findall(r"^vlan (\d+)(.*?)(?=^vlan |\n#)", self.raw_log, re.DOTALL | re.MULTILINE)
        for vid, content in vlan_blocks:
            vid = int(vid)
            self.data["vlans"][vid] = {"name": f"VLAN_{vid}", "ip": "", "mask": "", "ipv6": ""}
            d = re.search(r"description\s+(.+)", content)
            if d: self.data["vlans"][vid]["name"] = d.group(1).strip()

        # Interfaces
        interfaces = re.findall(r"^interface ([^\n]+)\n(.*?)(?=\n#)", self.raw_log, re.DOTALL | re.MULTILINE)
        for raw_name, cfg in interfaces:
            # Skip SVI: รองรับทั้ง Vlan-interface (Comware 5) และ Vlanif (H3C/Comware 7)
            if re.match(r'Vlan-?interface|Vlanif', raw_name.strip(), re.IGNORECASE): continue
            
            # The name might be a range like "1/1/4-1/1/21"
            # It might also have a type prefix like "GigabitEthernet1/0/1-GigabitEthernet1/0/5"
            expanded_names = self._expand_raw_interface_range(raw_name)

            for single_raw_name in expanded_names:
                port = self._map_interface_name(single_raw_name)
                if not port: continue

                iface = self._init_interface_data(cfg)
                d = re.search(r"description\s+(.+)", cfg)
                if d: iface["description"] = d.group(1).strip()

                # LAG Member
                m = re.search(r"port link-aggregation group (\d+)", cfg)
                if m:
                    iface["role"] = "lag_member"
                    iface["lag_id"] = m.group(1)
                    self.data["interfaces"][port] = iface
                    continue

                # Access & Trunk Logic (Comware)
                m = re.search(r"port access vlan (\d+)", cfg)
                if m:
                    iface["role"] = "access"
                    iface["access_vlan"] = int(m.group(1))

                if "port link-type trunk" in str(cfg):
                    iface["role"] = "trunk"
                    m = re.search(r"port trunk pvid vlan (\d+)", cfg)
                    iface["native_vlan"] = int(m.group(1)) if m else 1

                    # รวม VLAN จากทุก 'port trunk permit vlan' line
                    permit_lines = re.findall(r"port trunk permit vlan (.+)", cfg)
                    allowed: set = set()
                    for pl in permit_lines:
                        allowed |= self._parse_vlan_list(pl)

                    # ลบ VLAN ที่มี 'undo port trunk permit vlan'
                    undo_lines = re.findall(r"undo port trunk permit vlan (.+)", cfg)
                    for ul in undo_lines:
                        allowed -= self._parse_vlan_list(ul)

                    iface["allowed_vlans"] = allowed

                self.data["interfaces"][port] = iface

        # DHCP Pools (Comware)
        dhcp_pools = re.findall(r"^dhcp server ip-pool (\S+)\n(.*?)(?=^#|^dhcp server ip-pool)", self.raw_log, re.DOTALL | re.MULTILINE)
        for pool_name, pool_cfg in dhcp_pools:
            pool_data = {
                "name": pool_name,
                "network": "",
                "mask": "",
                "gateway": "",
                "dns": ""
            }
            n = re.search(r"network\s+(\S+)\s+mask\s+(\S+)", pool_cfg)
            if n:
                pool_data["network"] = n.group(1)
                pool_data["mask"] = n.group(2)
            
            g = re.search(r"gateway-list\s+(.*)", pool_cfg)
            if g: pool_data["gateway"] = g.group(1).strip()
            
            d = re.search(r"dns-list\s+(.*)", pool_cfg)
            if d: pool_data["dns"] = d.group(1).strip()
            
            self.data["dhcp_pools"].append(pool_data)

        # DHCP Relays (Comware) - Check routed ports first
        for raw_name, cfg in interfaces:
            # Match dhcp relay server-address <IP> or dhcp server apply ip-pool
            helpers = re.findall(r"dhcp relay server-address\s+(\S+)", cfg)
            if helpers:
                self.data["dhcp_relays"].append({
                    "interface": raw_name.strip(),
                    "helper_ips": helpers
                })

        # SVI & Helper Address for SVI (Comware)
        # รองรับทั้ง Comware 5 (Vlan-interface) และ H3C/Comware 7 (Vlanif)
        svis = re.findall(r"^interface (?:Vlan-interface|Vlanif)(\d+)\n(.*?)(?=\n#)", self.raw_log or "", re.DOTALL | re.MULTILINE | re.IGNORECASE)
        for vid, cfg in svis:
            self._parse_svi_ip(int(vid), cfg)
            helpers = re.findall(r"dhcp relay server-address\s+(\S+)", cfg)
            if helpers:
                self.data["dhcp_relays"].append({
                    "interface": f"Vlanif{vid}",
                    "helper_ips": helpers
                })

        # Routes
        routes = re.findall(r"ip route-static (\S+) (\S+) (\S+)", self.raw_log or "")
        for d, m, nh in routes: self.data["routes"].append({"version": "ipv4", "dest": d, "mask": m, "next_hop": nh})

        # IPv6 Routes
        ipv6_routes = re.findall(r"ipv6 route-static (\S+) (\S+) (\S+)", self.raw_log or "")
        for d, m, nh in ipv6_routes: self.data["routes"].append({"version": "ipv6", "dest": d, "mask": m, "next_hop": nh})

        # Global NTP (Comware often uses ntp-service unicast-server or ntp server)
        ntp_servers = re.findall(r"ntp(?:-service unicast-)?server\s+(\S+)", self.raw_log or "", re.IGNORECASE)
        for srv in ntp_servers:
            if srv not in self.data["ntp_servers"]:
                self.data["ntp_servers"].append(srv)

        # Global AAA commands (Comware)
        aaa_cmds = re.findall(r"^(aaa (?:authentication|authorization|accounting).*?)(?=\n)", self.raw_log or "", re.MULTILINE)
        for cmd in aaa_cmds:
            # We strip trailing 'local' if it exists just to maintain a cleaner normalized state, but let's keep it exact for now
            self.data["aaa_commands"].append(cmd.strip())

        # Global TACACS (Comware)
        tacacs_hosts = re.findall(r"^tacacs-server host\s+(\S+)", self.raw_log or "", re.MULTILINE)
        tacacs_keys = re.findall(r"^tacacs-server key\n([^\n]+)", self.raw_log or "", re.MULTILINE)
        # Often keys are on the next line or inline
        inline_keys = re.findall(r"^tacacs-server host\s+\S+\s+key (?:cipher |simple |)(\S+)", self.raw_log or "", re.MULTILINE)
        
        # Combine logic simply: if we find hosts, and we find a discrete key below it, associate them, else use inline
        for host in tacacs_hosts:
            key = tacacs_keys[0].strip() if tacacs_keys else (inline_keys[0].strip() if inline_keys else "")
            self.data["tacacs_servers"].append({"ip": host, "key": key})

        # Global RADIUS (Comware) -> radius scheme X -> primary authentication IP -> key
        radius_blocks = re.findall(r"^radius scheme (.*?)\n(.*?)(?=^radius scheme |^#|^!)", self.raw_log or "", re.DOTALL | re.MULTILINE)
        for name, block in radius_blocks:
            host_m = re.search(r"primary authentication\s+(\S+)", block)
            key_m = re.search(r"key authentication\s+(?:cipher |simple |)(\S+)", block)
            if host_m:
                self.data["radius_servers"].append({
                    "ip": host_m.group(1).strip(),
                    "key": key_m.group(1).strip() if key_m else ""
                })

        # SNMP (Comware)
        snmp_cmds = re.findall(r"^(snmp-agent.*?)(?=\n)", self.raw_log or "", re.MULTILINE)
        for cmd in snmp_cmds:
            self.data["snmp_commands"].append(cmd.strip())

    # ================= PARSER: CISCO IOS (เพิ่มใหม่) =================
    def _parse_cisco_ios(self):
        # Hostname
        m = re.search(r"^hostname\s+(\S+)", self.raw_log, re.MULTILINE)
        if m: self.data["hostname"] = m.group(1)

        # Banner
        banner_m = re.search(r"^banner motd\s+(.)(.*?)\1", self.raw_log, re.DOTALL | re.MULTILINE)
        if banner_m: self.data["banner"] = banner_m.group(2).strip()

        # VLAN Definitions (Cisco doesn't always show vlan config block if default)
        vlan_blocks = re.findall(r"^vlan (\d+)\n(.*?)(?=^vlan |^interface |^!)", self.raw_log, re.DOTALL | re.MULTILINE)
        for vid, content in vlan_blocks:
            vid = int(vid)
            self.data["vlans"][vid] = {"name": f"VLAN_{vid}", "ip": "", "mask": "", "ipv6": ""}
            d = re.search(r"name\s+(\S+)", content)
            if d: self.data["vlans"][vid]["name"] = d.group(1).strip()

        # Interfaces
        interfaces = re.findall(r"^interface ([^\n]+)\n(.*?)(?=^interface |^!)", self.raw_log, re.DOTALL | re.MULTILINE)
        for raw_name, cfg in interfaces:
            # Skip SVI here
            if raw_name.lower().startswith("vlan"): continue
            
            port = self._map_interface_name(raw_name)
            if not port: continue

            iface = self._init_interface_data(cfg)
            d = re.search(r"description\s+(.+)", cfg)
            if d: iface["description"] = d.group(1).strip()

            # LAG Member (channel-group 1 mode active)
            m = re.search(r"channel-group (\d+)", cfg)
            if m:
                iface["role"] = "lag_member"
                iface["lag_id"] = m.group(1)
                self.data["interfaces"][port] = iface
                continue

            # Switchport Mode
            mode_match = re.search(r"switchport mode (access|trunk)", cfg)
            mode = mode_match.group(1) if mode_match else "access" # Cisco default access usually
            
            # Check explicit trunk keywords
            if "switchport trunk" in cfg: mode = "trunk"

            if mode == "access":
                iface["role"] = "access"
                m = re.search(r"switchport access vlan (\d+)", cfg)
                iface["access_vlan"] = int(m.group(1)) if m else 1
            
            elif mode == "trunk":
                iface["role"] = "trunk"
                m = re.search(r"switchport trunk native vlan (\d+)", cfg)
                iface["native_vlan"] = int(m.group(1)) if m else 1
                
                m = re.search(r"switchport trunk allowed vlan ([\d,-]+)", cfg)
                if m: iface["allowed_vlans"] = self._parse_vlan_list(m.group(1))
            # OSPF Cost
            m = re.search(r"ip ospf cost (\d+)", cfg)
            if m: iface["ospf_cost"] = int(m.group(1))

            self.data["interfaces"][port] = iface

        # DHCP Pools (Cisco)
        dhcp_pools = re.findall(r"^ip dhcp pool (\S+)\n(.*?)(?=^!|^ip dhcp pool)", self.raw_log, re.DOTALL | re.MULTILINE)
        for pool_name, pool_cfg in dhcp_pools:
            pool_data = {
                "name": pool_name,
                "network": "",
                "mask": "",
                "gateway": "",
                "dns": ""
            }
            n = re.search(r"network\s+(\S+)\s+(\S+)", pool_cfg)
            if n:
                pool_data["network"] = n.group(1)
                pool_data["mask"] = n.group(2)
            
            g = re.search(r"default-router\s+(.*)", pool_cfg)
            if g: pool_data["gateway"] = g.group(1).strip()
            
            d = re.search(r"dns-server\s+(.*)", pool_cfg)
            if d: pool_data["dns"] = d.group(1).strip()
            
            self.data["dhcp_pools"].append(pool_data)

        # IP Helper Address / DHCP Relays (Cisco)
        # Search through already matched interfaces string
        for raw_name, cfg in interfaces:
            helpers = re.findall(r"ip helper-address\s+(\S+)", cfg)
            if helpers:
                self.data["dhcp_relays"].append({
                    "interface": raw_name.strip(),
                    "helper_ips": helpers
                })

        # SVI & Helper Address for SVI (Cisco)
        svis = re.findall(r"^interface Vlan(\d+)\n(.*?)(?=^interface |^!)", self.raw_log, re.DOTALL | re.MULTILINE)
        for vid, cfg in svis:
            self._parse_svi_ip(int(vid), cfg)
        # Routes
        routes = re.findall(r"^ip route (\S+) (\S+) (\S+)", self.raw_log or "", re.MULTILINE)
        for d, m, nh in routes: self.data["routes"].append({"version": "ipv4", "dest": d, "mask": m, "next_hop": nh})
        
        # IPv6 Routes
        ipv6_routes = re.findall(r"^ipv6 route (\S+) (\S+)", self.raw_log or "", re.MULTILINE)
        for d, nh in ipv6_routes:
            if "/" in d:
                dest, mask = d.split("/", 1)
                self.data["routes"].append({"version": "ipv6", "dest": dest, "mask": mask, "next_hop": nh})
            else:
                self.data["routes"].append({"version": "ipv6", "dest": d, "mask": "", "next_hop": nh})

        # Global NTP (Cisco)
        ntp_servers = re.findall(r"^ntp server\s+(\S+)", self.raw_log or "", re.MULTILINE)
        for srv in ntp_servers:
            if srv not in self.data["ntp_servers"]:
                self.data["ntp_servers"].append(srv)

        # Global AAA commands (Cisco)
        aaa_cmds = re.findall(r"^(aaa (?:new-model|authentication|authorization|accounting).*?)(?=\n)", self.raw_log or "", re.MULTILINE)
        for cmd in aaa_cmds:
            self.data["aaa_commands"].append(cmd.strip())

        # Global TACACS (Cisco legacy & modern)
        # legacy: tacacs-server host X key Y
        # modern: tacacs server NAME \n address ipv4 X \n key Y
        legacy_tacacs = re.findall(r"^tacacs-server host\s+(\S+)(?:\s+key\s+(\S+))?", self.raw_log or "", re.MULTILINE)
        for ip, key in legacy_tacacs:
            self.data["tacacs_servers"].append({"ip": ip, "key": key if key else ""})

        modern_tacacs = re.findall(r"^tacacs server \S+\n(.*?)address ipv4 (\S+)(.*?)(?=^!|^tacacs server|\Z)", self.raw_log or "", re.DOTALL | re.MULTILINE)
        for pre, ip, post in modern_tacacs:
            key_m = re.search(r"key\s+(\S+)", pre + post)
            self.data["tacacs_servers"].append({"ip": ip, "key": key_m.group(1) if key_m else ""})

        # Global RADIUS (Cisco)
        legacy_radius = re.findall(r"^radius-server host\s+(\S+)(?:\s+auth-port.*?)?(?:\s+key\s+(\S+))?", self.raw_log or "", re.MULTILINE)
        for ip, key in legacy_radius:
            self.data["radius_servers"].append({"ip": ip, "key": key if key else ""})

        # SNMP (Cisco)
        snmp_cmds = re.findall(r"^(snmp-server.*?)(?=\n)", self.raw_log or "", re.MULTILINE)
        for cmd in snmp_cmds:
            self.data["snmp_commands"].append(cmd.strip())

    # ================= SHARED HELPERS =================
    def _init_interface_data(self, cfg):
        return {
            "description": "", "role": None, "access_vlan": 1, 
            "native_vlan": 1, "allowed_vlans": set(), "lag_id": None,
            "shutdown": "shutdown" in cfg
        }

    def _parse_vlan_list(self, vlan_str):
        """ แปลง '1,10,20-30' หรือ '5 to 100' (Comware) เป็น set {1, 10, 20..30} """
        vids = set()
        # Normalize Comware 'X to Y' → 'X-Y' before splitting
        vlan_str = re.sub(r'(\d+)\s+to\s+(\d+)', lambda m: f"{m.group(1)}-{m.group(2)}", vlan_str)
        for part in vlan_str.replace(',', ' ').split():
            part = part.strip()
            if '-' in part:
                try:
                    s, e = map(int, part.split('-'))
                    vids.update(range(s, e + 1))
                except ValueError:
                    pass
            elif part.isdigit():
                vids.add(int(part))
        return vids

    def _parse_svi_ip(self, vid, cfg):
        self.data["vlans"].setdefault(vid, {"name": f"VLAN_{vid}", "ip": "", "mask": "", "ipv6": "", "description": ""})
        
        desc = re.search(r"description\s+(.+)", cfg)
        if desc:
            self.data["vlans"][vid]["description"] = desc.group(1).strip()
            
        m = re.search(r"ip address (\d+\.\d+\.\d+\.\d+) (\d+\.\d+\.\d+\.\d+)", cfg)
        if m:
            self.data["vlans"][vid]["ip"] = m.group(1)
            self.data["vlans"][vid]["mask"] = m.group(2)
        m6 = re.search(r"ipv6 address ([0-9a-fA-F:]+/\d+)", cfg)
        if m6:
            self.data["vlans"][vid]["ipv6"] = m6.group(1)

    # Helper: Expand a raw interface name that might be a range (e.g. "1/1/4-1/1/21" or "GigabitEthernet1/0/4-1/0/21")
    def _expand_raw_interface_range(self, raw_name):
        raw_name = raw_name.strip()
        # If it doesn't look like a range, return as is
        if "-" not in raw_name or " " in raw_name: # Simple exclude
            return [raw_name]
        
        try:
            # e.g., "1/1/4-1/1/21" or "Gig1/0/1-Gig1/0/5"
            parts = raw_name.split('-')
            if len(parts) == 2:
                start_p = parts[0].strip()
                end_p = parts[1].strip()

                # Extract common prefix and number part
                # Assume pattern like "prefix[number]"
                m_start = re.match(r"(.*?[\D/])(\d+)$", start_p)
                m_end = re.match(r"(.*?[\D/])(\d+)$", end_p)

                if m_start and m_end:
                    # e.g. "1/1/4" -> prefix="1/1/", num="4"
                    # "GigabitEthernet1/0/1" -> prefix="GigabitEthernet1/0/", num="1"
                    prefix_start = m_start.group(1)
                    num_start = int(m_start.group(2))
                    
                    prefix_end = m_end.group(1)
                    num_end = int(m_end.group(2))

                    # The prefixes might be different if the user typed "Gig1/0/1-1/0/5"
                    # But if the very last number changes, we can generate a range
                    # Let's assume the prefix of start_p is the base prefix if the end prefix matches or is shorter
                    # Specifically, if start_p is "1/1/4" and end_p is "1/1/21" -> prefix_start = "1/1/"
                    
                    # For safety, if prefixes don't match, we might just fall back to the start prefix
                    # We will output prefix_start + range(num_start, num_end+1)
                    if num_end >= num_start and num_end - num_start < 100: # sanity limit 100 ports
                        expanded = []
                        for i in range(num_start, num_end + 1):
                            expanded.append(f"{prefix_start}{i}")
                        return expanded
            return [raw_name]
        except:
            return [raw_name]

    def _map_interface_name(self, name):
        name = name.strip()
        
        # --- HPE Comware ---
        # Ten-GigabitEthernet1/1/1 -> map onto slot 1 (e.g., 1/1/49)
        m = re.match(r"(?:Ten-)?GigabitEthernet(\d+)/(\d+)/(\d+)", name)
        if m:
            member = int(m.group(1))
            module = int(m.group(2))
            port = int(m.group(3))
            
            # If it's the builtin module (0), keep port number
            if module == 0:
                return f"{member}/1/{port}"
            else:
                # If it's an expansion module, offset it to end of 48-port block
                # Module 1 starts at 49, Module 2 starts at 53 (assuming 4 ports per module)
                target_port = 48 + (module - 1) * 4 + port
                return f"{member}/1/{target_port}"

        # --- Cisco IOS (2960/Catalyst) ---
        # FastEthernet0/1 -> 1/1/1
        # GigabitEthernet0/1 -> 1/1/25 (สมมติว่าเป็น Uplink ต่อจาก Fa 24 ช่อง)
        # หรือถ้าเป็น Stack: Gi1/0/1 -> 1/1/1
        
        m = re.match(r"(FastEthernet|GigabitEthernet|TenGigabitEthernet)(\d+)/(\d+)", name)
        if m:
            # Cisco Standalone (0/1) or Stack Member (1/0/1)
            # กรณี 0/1 (Stack Member 0 -> 1, Slot 1)
            port = m.group(3)
            return f"1/1/{port}" # Map ง่ายๆ ไป Slot 1 หมดก่อน

        # LAG
        if name.startswith("Bridge-Aggregation") or name.startswith("Port-channel"):
            # ดึงเลขออกมา
            num = re.search(r"(\d+)$", name)
            return f"lag{num.group(1)}" if num else "lag1"

        return None

    def _iface_sort_key(self, name):
        if name.startswith("lag"):
            return (0, 0, 0, int(name.replace("lag", "")))
        try:
            parts = name.split("/")
            return (1, int(parts[0]), int(parts[1]), int(parts[2]))
        except:
            return (9, 0, 0, 0)

    # ================= GENERATOR (Aruba CX Ready-to-Paste) =================
    def _generate_aruba_cx_ready_to_paste(self):
        lines = []
        lines.append("configure terminal")
        lines.append("")
        lines.append(f"hostname {self.data['hostname']}")
        
        if self.data["banner"]:
            lines.append("banner motd #")
            lines.append(self.data["banner"])
            lines.append("#")
        lines.append("#")

        # NTP
        for srv in self.data.get("ntp_servers", []):
            lines.append(f"ntp server {srv}")
        if self.data.get("ntp_servers"):
            lines.append("ntp enable")
            lines.append("#")

        # AAA, TACACS, RADIUS (Put AAA stuff early before interfaces)
        tacacs = self.data.get("tacacs_servers", [])
        if tacacs:
            for t in tacacs:
                if t["key"]:
                    lines.append(f"tacacs-server host {t['ip']} key plaintext {t['key']}")
                else:
                    lines.append(f"tacacs-server host {t['ip']}")
            lines.append("#")

        radius = self.data.get("radius_servers", [])
        if radius:
            for r in radius:
                if r["key"]:
                    lines.append(f"radius-server host {r['ip']} key plaintext {r['key']}")
                else:
                    lines.append(f"radius-server host {r['ip']}")
            lines.append("#")

        # Dump raw AAA strings (they vary heavily, best effort is just appending them pre-interfaces)
        for cmd in self.data.get("aaa_commands", []):
            lines.append(cmd)
        if self.data.get("aaa_commands"):
            lines.append("#")

        # Dump raw SNMP commands (Comware snmp-agent or Cisco snmp-server)
        for cmd in self.data.get("snmp_commands", []):
            lines.append(cmd)
        if self.data.get("snmp_commands"):
            lines.append("#")

        # VLANs
        for vid in sorted(self.data["vlans"]):
            v = self.data["vlans"][vid]
            lines.append(f"vlan {vid}")
            lines.append(f'    name "{v["name"]}"')
            lines.append("    exit")
        lines.append("#")

        # SVI
        for vid in sorted(self.data["vlans"]):
            v = self.data["vlans"][vid]
            if v["ip"] or v["ipv6"] or v.get("description"):
                lines.append(f"interface vlan {vid}")
                if v.get("description"): lines.append(f"    description {v['description']}")
                if v["ip"]: lines.append(f"    ip address {v['ip']} {v['mask']}")
                if v["ipv6"]: lines.append(f"    ipv6 address {v['ipv6']}")
                lines.append("    exit")
                lines.append("#")

        # LAGs
        lags = set()
        for iface in self.data["interfaces"].values():
            if iface["lag_id"]: lags.add(iface["lag_id"])
        
        for lag_id in sorted(lags, key=lambda x: int(x)):
            lines.append(f"interface lag {lag_id}")
            lines.append("    no shutdown")
            lines.append("    no routing")
            lines.append("    lacp mode active")
            
            # Check if we parsed specific config for this LAG (e.g., from interface Bridge-Aggregation1)
            lag_port_name = f"lag{lag_id}"
            if lag_port_name in self.data["interfaces"]:
                conf = self.data["interfaces"][lag_port_name]
                
                if conf["description"]:
                    lines.append(f"    description {conf['description']}")

                if conf["role"] == "access":
                    lines.append(f"    vlan access {conf['access_vlan']}")
                elif conf["role"] == "trunk":
                    lines.append(f"    vlan trunk native {conf['native_vlan']}")
                    allowed = sorted(conf["allowed_vlans"])
                    if allowed: 
                        lines.append(f"    vlan trunk allowed {','.join(map(str, allowed))}")
                    else:
                        lines.append("    vlan trunk allowed all") # Fallback to all if it says trunk but no specific list
            else:
                lines.append("    vlan trunk native 1") # Default safe
                lines.append("    vlan trunk allowed all") # Default safe
                
            lines.append("    exit")
            lines.append("#")

        lines.append("")
        lines.append("! PHYSICAL_PORTS_START")
        
        # Physical Ports (No Grouping)
        phy_ports = [p for p in self.data["interfaces"] if not p.startswith("lag")]
        phy_ports.sort(key=self._iface_sort_key)

        for port in phy_ports:
            conf = self.data["interfaces"][port]
            
            lines.append(f"interface {port}")
            lines.append("    shutdown" if conf["shutdown"] else "    no shutdown")
            
            if conf["description"]:
                lines.append(f"    description {conf['description']}")

            if conf["role"] == "lag_member":
                lines.append(f"    lag {conf['lag_id']}")
            elif conf["role"] == "access":
                lines.append(f"    vlan access {conf['access_vlan']}")
            elif conf["role"] == "trunk":
                lines.append(f"    vlan trunk native {conf['native_vlan']}")
                allowed = sorted(conf["allowed_vlans"])
                if allowed: lines.append(f"    vlan trunk allowed {','.join(map(str, allowed))}")
            
            lines.append("    exit")
            lines.append("#")

        lines.append("! PHYSICAL_PORTS_END")
        lines.append("")

        for r in self.data["routes"]:
            v = r.get("version", "ipv4")
            if v == "ipv4":
                lines.append(f"ip route {r['dest']} {r['mask']} {r['next_hop']}")
            elif v == "ipv6":
                # Comware IPv6 Route: dest=:: mask=0 next_hop=2001:...
                mask = r['mask']
                if mask.isdigit():
                    lines.append(f"ipv6 route {r['dest']}/{mask} {r['next_hop']}")
                else:
                    lines.append(f"ipv6 route {r['dest']} {r['mask']} {r['next_hop']}")

        lines.append("end")
        lines.append("write memory")
        
        return "\n".join(lines)