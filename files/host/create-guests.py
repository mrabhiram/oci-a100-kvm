#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Create two CPU-only guests and private VNIC links after a complete preflight."""
import argparse
import grp
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import urllib.request
import uuid
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from config import root_config, trusted_path

OVMF_CODE = Path('/usr/share/OVMF/OVMF_CODE_4M.fd')
OVMF_VARS = Path('/usr/share/OVMF/OVMF_VARS_4M.fd')
LINK_HELPER = Path('/usr/local/sbin/oci-kvm-ensure-link')
QEMU_NS = 'http://libvirt.org/schemas/domain/qemu/1.0'
ET.register_namespace('qemu', QEMU_NS)


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def run(*args, stdin=None, timeout=60):
    return subprocess.run([str(arg) for arg in args], input=stdin, text=True,
                          check=True, capture_output=True, timeout=timeout).stdout


def cpuset(value):
    result = set()
    for item in value.strip().split(','):
        bounds = [int(v) for v in item.split('-')]
        require(len(bounds) in (1, 2), 'Invalid Linux CPU list.')
        result.update(range(bounds[0], bounds[-1] + 1))
    return result


def stream_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def check_management_overlap(config, addresses, routes):
    """Default routes are expected; all more specific overlap is rejected."""
    management = ipaddress.ip_interface(config['management']['host_cidr']).network
    networks = [ipaddress.ip_interface(config['host']['primary_cidr']).network]
    for guest in config['guests']:
        networks.append(ipaddress.ip_network(f"{guest['vnic']['ip']}/{guest['vnic']['prefix']}", strict=False))
    for iface in addresses:
        networks.extend(ipaddress.ip_network(f"{a['local']}/{a['prefixlen']}", strict=False)
                        for a in iface.get('addr_info', []) if a.get('family') == 'inet')
    for route in routes:
        destination = route.get('dst', 'default')
        if destination not in ('default', '0.0.0.0/0'):
            networks.append(ipaddress.ip_network(destination, strict=False))
    require(not any(n.version == management.version and n.overlaps(management) for n in networks),
            'Management subnet overlaps a host address, VNIC subnet or existing route.')


def check_topology(config, topology, memory, available_mib):
    """Require true SMT pairs and enough free memory on each selected host NUMA node."""
    required = {}
    maps = {}
    guest_cpus = {cpu for guest in config['guests'] for cpu in guest['cpu_pins']}
    emulator_seen = set()
    pin_seen = set()
    require(config['host']['reserve_memory_gib'] > 0, 'Host RAM reserve must be positive.')
    reserve_mib = config['host']['reserve_memory_gib'] * 1024
    for guest in config['guests']:
        pins = guest['cpu_pins']
        require(pins and len(pins) % 2 == 0 and not set(pins) & pin_seen,
                'Guests need distinct, even-length CPU pin lists.')
        pin_seen.update(pins)
        nodes = guest['numa_nodes']
        require(nodes and len(nodes) == len(set(nodes)) and all(n in memory for n in nodes),
                'A selected NUMA node is absent or duplicated.')
        require(guest['memory_mib'] % len(nodes) == 0, 'Guest memory must divide equally across its NUMA nodes.')
        node_vcpus = {node: [] for node in nodes}
        for vcpu, cpu in enumerate(pins):
            require(cpu in topology, f'CPU {cpu} is offline or missing.')
            require(topology[cpu]['node'] in nodes, 'CPU pin is outside the guest NUMA nodes.')
            node_vcpus[topology[cpu]['node']].append(vcpu)
        for index in range(0, len(pins), 2):
            pair = set(pins[index:index + 2])
            require(len(pair) == 2 and all(topology[c]['siblings'] == pair for c in pair),
                    'Consecutive CPU pins must be both threads of one physical SMT core.')
        require(all(node_vcpus.values()), 'Each configured NUMA node must have guest CPUs.')
        own = set(pins)
        emulators = set(guest['emulator_cpus'])
        require(emulators and not emulators & ((guest_cpus - own) | emulator_seen),
                'Emulator CPUs overlap another guest.')
        require(all(c in topology and topology[c]['node'] in nodes for c in emulators),
                'Emulator CPUs must be online and within the guest NUMA nodes.')
        emulator_seen.update(emulators)
        per_node = guest['memory_mib'] // len(nodes)
        for node in nodes:
            required[node] = required.get(node, 0) + per_node
        maps[guest['name']] = node_vcpus
    reserve_per_node = (reserve_mib + len(memory) - 1) // len(memory)
    for node, amount in required.items():
        require(memory[node]['free_mib'] >= amount + reserve_per_node,
                f'NUMA node {node} lacks free RAM plus its share of the host reserve.')
    require(available_mib >= sum(required.values()) + reserve_mib,
            'Host available RAM is below guest allocations plus the host reserve.')
    return maps


def read_topology():
    cpu_root = Path('/sys/devices/system/cpu')
    online = cpuset((cpu_root / 'online').read_text())
    topology = {}
    for cpu in online:
        root = cpu_root / f'cpu{cpu}'
        nodes = list(root.glob('node[0-9]*'))
        require(len(nodes) == 1, 'Cannot resolve CPU NUMA membership.')
        topology[cpu] = {'node': int(nodes[0].name[4:]),
                         'siblings': cpuset((root / 'topology/thread_siblings_list').read_text())}
    memory = {}
    for node in Path('/sys/devices/system/node').glob('node[0-9]*'):
        fields = dict(re.findall(r'(MemTotal|MemFree):\s+(\d+) kB', (node / 'meminfo').read_text()))
        memory[int(node.name[4:])] = {'total_mib': int(fields['MemTotal']) // 1024,
                                     'free_mib': int(fields['MemFree']) // 1024}
    available = re.search(r'^MemAvailable:\s+(\d+) kB$', Path('/proc/meminfo').read_text(), re.M)
    require(available is not None, 'Cannot read available host memory.')
    return topology, memory, int(available[1]) // 1024


def metadata(endpoint):
    request = urllib.request.Request('http://169.254.169.254/opc/v2/' + endpoint,
                                     headers={'Authorization': 'Bearer Oracle'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=15) as response:
        return json.load(response)


def check_vnics(config, vnics, addresses, links):
    host = config['host']
    parent = next((v for v in links if v['ifname'] == host['interface']), None)
    require(parent and not parent.get('linkinfo', {}).get('info_kind'), 'Physical parent NIC not found.')
    require(parent['mtu'] >= host['mtu'], 'Configured guest MTU exceeds the physical NIC MTU.')
    host_ip = ipaddress.ip_interface(host['primary_cidr'])
    parent_ips = {f"{a['local']}/{a['prefixlen']}" for iface in addresses
                  if iface['ifname'] == host['interface'] for a in iface.get('addr_info', [])}
    require(str(host_ip) in parent_ips, 'Host primary address does not match configuration.')
    require(any(v.get('privateIp') == str(host_ip.ip) and v.get('nicIndex') == host['nic_index']
                for v in vnics), 'Host NIC index cannot be verified in OCI metadata.')
    for guest in config['guests']:
        vnic = guest['vnic']
        require(vnic['nic_index'] == host['nic_index'], 'Guest VNIC uses a different physical NIC.')
        matches = [v for v in vnics if v.get('vnicId') == vnic['id']
                   and v.get('privateIp') == vnic['ip'] and v.get('macAddr', '').lower() == vnic['mac']
                   and v.get('vlanTag') == vnic['vlan_tag'] and v.get('nicIndex') == vnic['nic_index']
                   and v.get('virtualRouterIp') == vnic['gateway']
                   and ipaddress.ip_network(v['subnetCidrBlock']) == ipaddress.ip_network(
                       f"{vnic['ip']}/{vnic['prefix']}", strict=False)]
        require(len(matches) == 1, f"{guest['name']}: VNIC metadata identity differs from configuration.")
        require(not any(a.get('local') == vnic['ip'] for i in addresses for a in i.get('addr_info', [])),
                'Guest VNIC address is already configured on the host.')
        for link in links:
            details = link.get('linkinfo', {})
            require(link['ifname'] != f"kv{vnic['vlan_tag']}", 'Target Linux VNIC link already exists.')
            require(not (details.get('info_kind') == 'vlan'
                         and details.get('info_data', {}).get('id') == vnic['vlan_tag']
                         and link.get('link_index') == parent['ifindex']), 'VLAN parent/tag is already used.')


def render_management(config):
    interface = ipaddress.ip_interface(config['management']['host_cidr'])
    root = ET.Element('network')
    ET.SubElement(root, 'name').text = config['management']['name']
    ET.SubElement(root, 'bridge', name=config['management']['bridge'], stp='on', delay='0')
    ET.SubElement(root, 'dns', enable='no')
    ET.SubElement(root, 'ip', address=str(interface.ip), netmask=str(interface.network.netmask))
    return ET.tostring(root, encoding='unicode') + '\n'


def render_domain(config, guest, node_vcpus, emulator):
    out = Path(config['paths']['image_root']) / guest['name']
    root = ET.Element('domain', type='kvm')
    ET.SubElement(root, 'name').text = guest['name']
    ET.SubElement(root, 'memory', unit='MiB').text = str(guest['memory_mib'])
    ET.SubElement(root, 'currentMemory', unit='MiB').text = str(guest['memory_mib'])
    ET.SubElement(root, 'vcpu', placement='static').text = str(len(guest['cpu_pins']))
    tuning = ET.SubElement(root, 'cputune')
    for index, cpu in enumerate(guest['cpu_pins']):
        ET.SubElement(tuning, 'vcpupin', vcpu=str(index), cpuset=str(cpu))
    ET.SubElement(tuning, 'emulatorpin', cpuset=','.join(map(str, guest['emulator_cpus'])))
    numa_tune = ET.SubElement(root, 'numatune')
    ET.SubElement(numa_tune, 'memory', mode='strict', nodeset=','.join(map(str, guest['numa_nodes'])))
    os_xml = ET.SubElement(root, 'os')
    ET.SubElement(os_xml, 'type', arch='x86_64', machine='q35').text = 'hvm'
    ET.SubElement(os_xml, 'loader', readonly='yes', secure='no', type='pflash').text = str(OVMF_CODE)
    ET.SubElement(os_xml, 'nvram', template=str(OVMF_VARS)).text = f"/var/lib/libvirt/qemu/nvram/{guest['name']}_VARS.fd"
    ET.SubElement(os_xml, 'boot', dev='hd')
    features = ET.SubElement(root, 'features')
    ET.SubElement(features, 'acpi')
    ET.SubElement(features, 'apic')
    cpu = ET.SubElement(root, 'cpu', mode='host-passthrough', check='none', migratable='off')
    ET.SubElement(cpu, 'topology', sockets='1', cores=str(len(guest['cpu_pins']) // 2), threads='2')
    ET.SubElement(cpu, 'maxphysaddr', mode='passthrough')
    ET.SubElement(cpu, 'feature', policy='require', name='topoext')
    numa = ET.SubElement(cpu, 'numa')
    for index, node in enumerate(guest['numa_nodes']):
        ET.SubElement(numa, 'cell', id=str(index), cpus=','.join(map(str, node_vcpus[node])),
                      memory=str(guest['memory_mib'] // len(guest['numa_nodes'])), unit='MiB')
        ET.SubElement(numa_tune, 'memnode', cellid=str(index), mode='strict', nodeset=str(node))
    ET.SubElement(root, 'clock', offset='utc')
    ET.SubElement(root, 'on_poweroff').text = 'destroy'
    ET.SubElement(root, 'on_reboot').text = 'restart'
    ET.SubElement(root, 'on_crash').text = 'destroy'
    devices = ET.SubElement(root, 'devices')
    ET.SubElement(devices, 'emulator').text = emulator
    for filename, device, target, bus, fmt in (
        ('root.qcow2', 'disk', 'vda', 'virtio', 'qcow2'),
        ('seed.img', 'cdrom', 'sda', 'sata', 'raw')):
        disk = ET.SubElement(devices, 'disk', type='file', device=device)
        ET.SubElement(disk, 'driver', name='qemu', type=fmt)
        ET.SubElement(disk, 'source', file=str(out / filename))
        ET.SubElement(disk, 'target', dev=target, bus=bus)
        if device == 'cdrom':
            ET.SubElement(disk, 'readonly')
    ET.SubElement(devices, 'controller', type='pci', index='0', model='pcie-root')
    for index in range(1, 5):
        ET.SubElement(devices, 'controller', type='pci', index=str(index), model='pcie-root-port')
    ET.SubElement(devices, 'controller', type='sata', index='0')
    ET.SubElement(devices, 'controller', type='usb', model='none')
    vnic = guest['vnic']
    interface = ET.SubElement(devices, 'interface', type='direct')
    ET.SubElement(interface, 'mac', address=vnic['mac'])
    ET.SubElement(interface, 'source', dev=f"kv{vnic['vlan_tag']}", mode='passthrough')
    ET.SubElement(interface, 'model', type='virtio')
    interface = ET.SubElement(devices, 'interface', type='network')
    ET.SubElement(interface, 'mac', address=guest['management']['mac'])
    ET.SubElement(interface, 'source', network=config['management']['name'])
    ET.SubElement(interface, 'model', type='virtio')
    serial = ET.SubElement(devices, 'serial', type='pty')
    ET.SubElement(serial, 'target', type='isa-serial', port='0')
    console = ET.SubElement(devices, 'console', type='pty')
    ET.SubElement(console, 'target', type='serial', port='0')
    ET.SubElement(devices, 'memballoon', model='none')
    commandline = ET.SubElement(root, f'{{{QEMU_NS}}}commandline')
    for value in ('-fw_cfg', 'name=opt/ovmf/X-PciMmio64Mb,string=524288',
                  '-global', 'q35-pcihost.pci-hole64-size=512G'):
        ET.SubElement(commandline, f'{{{QEMU_NS}}}arg', value=value)
    ET.indent(root)
    return ET.tostring(root, encoding='unicode') + '\n'


def render_cloud_init(config, guest, public_key):
    userdata = {'hostname': guest['name'], 'manage_etc_hosts': True, 'disable_root': True,
                'ssh_pwauth': False, 'users': [{'name': 'ubuntu', 'groups': ['adm', 'sudo'],
                    'shell': '/bin/bash', 'sudo': 'ALL=(ALL) NOPASSWD:ALL', 'lock_passwd': True,
                    'ssh_authorized_keys': [public_key]}], 'package_update': False, 'package_upgrade': False}
    prefix = ipaddress.ip_interface(config['management']['host_cidr']).network.prefixlen
    vnic = guest['vnic']
    ethernets = {}
    for name, mac, address in (
        ('mgmt0', guest['management']['mac'], f"{guest['management']['ip']}/{prefix}"),
        ('vcn0', vnic['mac'], f"{vnic['ip']}/{vnic['prefix']}")):
        ethernets[name] = {'match': {'macaddress': mac}, 'set-name': name, 'dhcp4': False,
                          'dhcp6': False, 'accept-ra': False, 'link-local': [],
                          'mtu': config['host']['mtu'] if name == 'vcn0' else 1500,
                          'addresses': [address]}
    ethernets['vcn0']['routes'] = [{'to': '0.0.0.0/0', 'via': vnic['gateway']}]
    ethernets['vcn0']['nameservers'] = {'addresses': ['169.254.169.254']}
    return {'user-data': '#cloud-config\n' + json.dumps(userdata, indent=2) + '\n',
            'meta-data': json.dumps({'instance-id': guest['name'] + '-' + str(uuid.uuid4()),
                                     'local-hostname': guest['name']}) + '\n',
            'network-config': json.dumps({'version': 2, 'ethernets': ethernets}, indent=2) + '\n'}


def network_files(config, guest):
    name = guest['name']
    vnic = guest['vnic']
    interface = f"kv{vnic['vlan_tag']}"
    spec = {'parent': config['host']['interface'], 'interface': interface, 'tag': vnic['vlan_tag'],
            'mac': vnic['mac'], 'vnic_id': vnic['id'], 'ip': vnic['ip'], 'prefix': vnic['prefix'],
            'gateway': vnic['gateway'], 'nic_index': vnic['nic_index'], 'mtu': config['host']['mtu'],
            'primary_cidr': config['host']['primary_cidr']}
    return {
        Path(f'/etc/oci-private-kvm/{name}.json'): (json.dumps(spec, indent=2) + '\n', 0o600),
        Path(f'/etc/systemd/network/05-oci-kvm-{name}.network'): (
            f'[Match]\nName={interface}\n[Link]\nRequiredForOnline=no\n'
            '[Network]\nDHCP=no\nLinkLocalAddressing=no\nIPv6AcceptRA=no\n', 0o644),
        Path(f'/etc/systemd/system/oci-kvm-net-{name}.service'): (
            f'[Unit]\nDescription=OCI guest VNIC for {name}\nWants=network-online.target\n'
            'After=network-online.target\nBefore=libvirtd.service virtqemud.service libvirt-guests.service\n'
            '[Service]\nType=oneshot\nRemainAfterExit=yes\n'
            f'ExecStart={LINK_HELPER} /etc/oci-private-kvm/{name}.json\n'
            '[Install]\nWantedBy=multi-user.target\n', 0o644)}


def preflight(config):
    require(os.geteuid() == 0, 'Run preflight and execution as root.')
    for key in ('base_dir', 'image_root'):
        trusted_path(config['paths'][key], allow_missing=True, directory=True)
    trusted_path(config['paths']['image'])
    trusted_path(config['ssh_public_key'])
    for binary in ('virsh', 'qemu-img', 'qemu-system-x86_64', 'cloud-localds', 'ip', 'xmllint',
                   'systemctl', 'networkctl', 'ssh-keygen'):
        require(shutil.which(binary), f'Missing command: {binary}')
    for path in (OVMF_CODE, OVMF_VARS, Path('/dev/kvm')):
        require(path.exists(), f'Missing prerequisite: {path}')
    require('AuthenticAMD' in Path('/proc/cpuinfo').read_text(), 'This build requires an AMD host.')
    require(run('systemctl', 'is-active', 'systemd-networkd').strip() == 'active',
            'systemd-networkd must be active.')
    require(metadata('instance/')['shape'] == 'BM.GPU4.8', 'OCI metadata shape must be BM.GPU4.8.')
    existing = set(run('virsh', 'list', '--all', '--name').split())
    require(not existing.intersection(g['name'] for g in config['guests']),
            'A requested domain already exists; refusing to overwrite or resume a partial build.')
    management = config['management']
    require(management['name'] not in run('virsh', 'net-list', '--all', '--name').split(),
            'The management network already exists; inspect it before proceeding.')
    addresses = json.loads(run('ip', '-j', 'address', 'show'))
    links = json.loads(run('ip', '-j', '-d', 'link', 'show'))
    require(not any(i['ifname'] == management['bridge'] for i in links), 'Management bridge already exists.')
    check_management_overlap(config, addresses, json.loads(run('ip', '-j', '-4', 'route', 'show', 'table', 'all')))
    check_vnics(config, metadata('vnics/'), addresses, links)
    require(Path('/sys/class/net', config['host']['interface'], 'device').exists(), 'Parent NIC is not physical.')
    maps = check_topology(config, *read_topology())
    image = Path(config['paths']['image'])
    require(image.is_file(), 'Ubuntu cloud image is missing.')
    require(stream_sha256(image) == config['paths']['image_sha256'].lower(), 'Cloud image SHA256 mismatch.')
    image_info = json.loads(run('qemu-img', 'info', '--output=json', image))
    require(image_info.get('format') == 'qcow2' and not image_info.get('backing-filename')
            and not image_info.get('encrypted'), 'Use a standalone, unencrypted qcow2 Ubuntu image.')
    key_path = Path(config['ssh_public_key'])
    require(key_path.is_file(), 'SSH public key file is missing.')
    key = key_path.read_text().strip()
    require(len(key.splitlines()) == 1 and key.startswith(('ssh-ed25519 ', 'ssh-rsa ', 'ecdsa-sha2-')),
            'Provide exactly one SSH public key, not a private key.')
    run('ssh-keygen', '-l', '-f', key_path)  # Check structure without printing the key or fingerprint.
    uid, gid = pwd.getpwnam('libvirt-qemu').pw_uid, grp.getgrnam('kvm').gr_gid
    helper_source = Path(__file__).resolve().parents[1] / 'network/ensure-vnic-link.sh'
    require(helper_source.is_file(), 'Bundled network helper is missing.')
    trusted_path(LINK_HELPER, allow_missing=True)
    if LINK_HELPER.exists():
        require(not LINK_HELPER.is_symlink() and LINK_HELPER.read_bytes() == helper_source.read_bytes(),
                'An existing network helper differs. Review it before installing this build.')
    filesystem = Path(config['paths']['image_root'])
    while not filesystem.exists():
        filesystem = filesystem.parent
    free = shutil.disk_usage(filesystem).free
    require(free >= (sum(g['disk_gib'] for g in config['guests']) + 10) * 1024**3,
            'Storage must have room for full guest disk sizes plus 10 GiB.')
    files = {}
    domains = {}
    for guest in config['guests']:
        out = Path(config['paths']['image_root']) / guest['name']
        require(not out.exists() and not out.is_symlink(), f'Guest storage already exists: {out}')
        require(guest['disk_gib'] * 1024**3 >= image_info['virtual-size'], 'Guest disk is smaller than source image.')
        nvram = Path('/var/lib/libvirt/qemu/nvram') / f"{guest['name']}_VARS.fd"
        require(not nvram.exists() and not nvram.is_symlink(), 'Guest UEFI variable storage already exists.')
        files.update(network_files(config, guest))
        domain = render_domain(config, guest, maps[guest['name']], shutil.which('qemu-system-x86_64'))
        run('xmllint', '--noout', '--relaxng', '/usr/share/libvirt/schemas/domain.rng', '-', stdin=domain)
        domains[guest['name']] = domain
    for path in files:
        trusted_path(path, allow_missing=True)
        require(not path.exists() and not path.is_symlink(), f'Configuration file already exists: {path}')
    network_path = Path(config['paths']['base_dir']) / 'management-network.xml'
    require(not network_path.exists() and not network_path.is_symlink(), 'Generated network XML already exists.')
    network_xml = render_management(config)
    run('xmllint', '--noout', '--relaxng', '/usr/share/libvirt/schemas/network.rng', '-', stdin=network_xml)
    return {'uid': uid, 'gid': gid, 'key': key, 'files': files, 'domains': domains,
            'helper_source': helper_source, 'network_path': network_path, 'network_xml': network_xml}


def write_new(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        stream.write(content)
    path.chmod(mode)


def execute(config, plan):
    print('Preflight passed. Creating network links and CPU-only guests.', flush=True)
    if not LINK_HELPER.exists():
        write_new(LINK_HELPER, plan['helper_source'].read_text(), 0o755)
    for path, (content, mode) in plan['files'].items():
        write_new(path, content, mode)
    run('networkctl', 'reload')
    run('systemctl', 'daemon-reload')
    for guest in config['guests']:
        run('systemctl', 'enable', '--now', f"oci-kvm-net-{guest['name']}.service")
    write_new(plan['network_path'], plan['network_xml'], 0o600)
    run('virsh', 'net-define', plan['network_path'])
    run('virsh', 'net-start', config['management']['name'])
    run('virsh', 'net-autostart', config['management']['name'])
    for guest in config['guests']:
        out = Path(config['paths']['image_root']) / guest['name']
        out.mkdir(parents=True, mode=0o750)
        for filename, content in render_cloud_init(config, guest, plan['key']).items():
            write_new(out / filename, content, 0o640)
        run('qemu-img', 'convert', '-f', 'qcow2', '-O', 'qcow2', config['paths']['image'],
            out / 'root.qcow2', timeout=900)
        run('qemu-img', 'resize', out / 'root.qcow2', f"{guest['disk_gib']}G")
        run('cloud-localds', '--network-config', out / 'network-config', out / 'seed.img',
            out / 'user-data', out / 'meta-data')
        write_new(out / 'domain.xml', plan['domains'][guest['name']], 0o640)
        for path in [out, *out.iterdir()]:
            os.chown(path, plan['uid'], plan['gid'])
            path.chmod(0o750 if path.is_dir() else 0o640)
        run('virsh', 'define', '--validate', out / 'domain.xml')
        run('virsh', 'autostart', '--disable', guest['name'])
        run('virsh', 'start', guest['name'], timeout=180)
        print(f"{guest['name']}: CPU-only guest started; GPU assignment is a later step.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--execute', action='store_true', help='Create after all preflight checks pass.')
    args = parser.parse_args()
    config = root_config(args.config)
    plan = preflight(config)
    if args.execute:
        execute(config, plan)
    else:
        print('Preflight passed. No changes made. Add --execute to create these CPU-only guests:')
        for guest in config['guests']:
            print(f"  {guest['name']}: {len(guest['cpu_pins'])} vCPUs, {guest['memory_mib']} MiB RAM, "
                  f"{guest['disk_gib']} GiB disk, private VNIC + isolated management interface")


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as error:
        # Command output excludes credentials and cloud-init public key contents.
        print(f'ERROR: {error.cmd[0]} failed (exit {error.returncode}): {error.stderr.strip()}', file=sys.stderr)
        print('No automatic rollback is attempted. Inspect any partial resources before retrying.', file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print(f'ERROR: {error}', file=sys.stderr)
        print('No automatic rollback is attempted. Inspect any partial resources before retrying.', file=sys.stderr)
        sys.exit(1)
