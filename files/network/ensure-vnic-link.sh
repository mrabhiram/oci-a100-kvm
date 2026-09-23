#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Systemd calls this at each boot. No host address or route is added.
set -euo pipefail
exec python3 - "$@" <<'PY'
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import urllib.request

def require(ok, message):
    if not ok:
        raise SystemExit(message)

def run(*args):
    return subprocess.check_output(args, text=True, timeout=30)

require(os.geteuid() == 0 and len(sys.argv) == 2,
        'Run as root with one generated VNIC specification path.')
path = Path(sys.argv[1])
info = path.stat()
require(not path.is_symlink() and info.st_uid == 0 and not info.st_mode & 0o022,
        'The VNIC specification must be root-owned and not writable by others.')
require(stat.S_ISREG(info.st_mode), 'The VNIC specification must be a regular file.')
s = json.loads(path.read_text())
for field in ('parent', 'interface'):
    require(re.fullmatch(r'[a-zA-Z0-9_.-]{1,15}', s[field]), 'Invalid interface name.')
require(type(s['tag']) is int and 1 <= s['tag'] <= 4094, 'Invalid VLAN tag.')
require(type(s['nic_index']) is int and s['nic_index'] >= 0, 'Invalid NIC index.')
require(type(s['mtu']) is int and 1280 <= s['mtu'] <= 9000, 'Invalid MTU.')
require(re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', s['mac']), 'Invalid MAC.')
primary = ipaddress.ip_interface(s['primary_cidr'])
guest = ipaddress.ip_interface(f"{s['ip']}/{s['prefix']}")
require(primary.version == guest.version == 4, 'IPv4 is required.')
require(Path('/sys/class/net', s['parent'], 'device').exists(), 'Physical NIC missing.')
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
request = urllib.request.Request('http://169.254.169.254/opc/v2/vnics/',
                                 headers={'Authorization': 'Bearer Oracle'})
with opener.open(request, timeout=15) as response:
    vnics = json.load(response)
matches = [v for v in vnics if v.get('vnicId') == s['vnic_id']
           and v.get('nicIndex') == s['nic_index'] and v.get('vlanTag') == s['tag']
           and v.get('macAddr', '').lower() == s['mac']
           and v.get('privateIp') == s['ip'] and v.get('virtualRouterIp') == s['gateway']
           and ipaddress.ip_network(v['subnetCidrBlock']) == guest.network]
require(len(matches) == 1, 'VNIC metadata identity changed; refusing link setup.')
addresses = json.loads(run('ip', '-j', 'address', 'show'))
links = json.loads(run('ip', '-j', '-d', 'link', 'show'))
parent = next((v for v in links if v['ifname'] == s['parent']), None)
require(parent and not parent.get('linkinfo', {}).get('info_kind'), 'Invalid physical parent.')
require(parent['mtu'] >= s['mtu'], 'Parent MTU is too small.')
parent_addresses = {f"{a['local']}/{a['prefixlen']}" for v in addresses
                    if v['ifname'] == s['parent'] for a in v.get('addr_info', [])}
require(str(primary) in parent_addresses, 'Host primary address changed.')
require(any(v.get('nicIndex') == s['nic_index'] and v.get('vnicId') != s['vnic_id']
            and v.get('privateIp') == str(primary.ip) for v in vnics),
        'Physical NIC index cannot be verified against the host address.')
require(not any(a.get('local') == s['ip'] for v in addresses for a in v.get('addr_info', [])),
        'Guest address is already configured on the host.')
existing = None
for link in links:
    details = link.get('linkinfo', {})
    same = (details.get('info_kind') == 'vlan'
            and details.get('info_data', {}).get('id') == s['tag']
            and link.get('link_index') == parent['ifindex'])
    require(not same or link['ifname'] == s['interface'], 'Parent/tag already used by another link.')
    if link['ifname'] == s['interface']:
        require(same and link['address'].lower() == s['mac'] and link['mtu'] == s['mtu'],
                'Existing link identity or MTU differs; refusing changes.')
        require(not any(v.get('addr_info') for v in addresses if v['ifname'] == s['interface']),
                'Existing guest link has a host address; refusing changes.')
        existing = link
if existing is None:
    run('ip', 'link', 'add', 'link', s['parent'], 'name', s['interface'],
        'address', s['mac'], 'mtu', str(s['mtu']), 'type', 'vlan', 'id', str(s['tag']))
run('ip', 'link', 'set', 'dev', s['interface'], 'up')
print(f"Verified guest VNIC link {s['interface']} on {s['parent']}.")
PY
