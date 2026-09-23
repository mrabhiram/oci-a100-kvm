# SPDX-License-Identifier: MIT
"""Offline checks use synthetic identities and never contact OCI or libvirt."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('provisioning', ROOT / 'files/host/create-guests.py')
provisioning = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provisioning)


def fixture():
    return {
        'host': {'interface': 'eth0', 'nic_index': 1, 'primary_cidr': '192.0.2.10/24',
                 'mtu': 1500, 'reserve_memory_gib': 32},
        'paths': {'image_root': '/var/lib/libvirt/images'},
        'management': {'name': 'gpu-mgmt', 'bridge': 'virbr90', 'host_cidr': '198.18.0.1/24'},
        'guests': [{
            'name': f'gpu-vm{n+1}', 'memory_mib': 32768, 'disk_gib': 200,
            'cpu_pins': [n * 4, n * 4 + 8, n * 4 + 1, n * 4 + 9],
            'emulator_cpus': [n * 4 + 2, n * 4 + 10], 'numa_nodes': [n],
            'management': {'ip': f'198.18.0.{n+11}', 'mac': f'52:54:00:00:00:{n+1:02x}'},
            'vnic': {'id': f'ocid1.vnic.oc1.example.synthetic{n}', 'ip': f'192.0.2.{n+20}',
                     'prefix': 24, 'gateway': '192.0.2.1', 'mac': f'02:00:00:00:01:{n+1:02x}',
                     'vlan_tag': n + 100, 'nic_index': 1}}
            for n in range(2)]}


def host_topology():
    topology = {cpu: {'node': (cpu % 8) // 4, 'siblings': {cpu % 8, cpu % 8 + 8}}
                for cpu in range(16)}
    memory = {node: {'total_mib': 131072, 'free_mib': 120000} for node in range(2)}
    return topology, memory, 240000


def host_network(config):
    host = config['host']
    links = [{'ifname': 'eth0', 'ifindex': 2, 'mtu': 9000}]
    addresses = [{'ifname': 'eth0', 'addr_info': [{'family': 'inet', 'local': '192.0.2.10', 'prefixlen': 24}]}]
    metadata = [{'vnicId': 'ocid1.vnic.oc1.example.primary', 'privateIp': '192.0.2.10',
                 'nicIndex': host['nic_index']}]
    for guest in config['guests']:
        vnic = guest['vnic']
        metadata.append({'vnicId': vnic['id'], 'privateIp': vnic['ip'], 'macAddr': vnic['mac'],
                         'vlanTag': vnic['vlan_tag'], 'nicIndex': vnic['nic_index'],
                         'virtualRouterIp': vnic['gateway'], 'subnetCidrBlock': '192.0.2.0/24'})
    return metadata, addresses, links


class ProvisioningTests(unittest.TestCase):
    def test_default_route_is_not_subnet_overlap(self):
        config = fixture()
        _, addresses, _ = host_network(config)
        provisioning.check_management_overlap(config, addresses, [{'dst': 'default'}, {'dst': '192.0.2.0/24'}])

    def test_broader_and_narrower_routes_are_overlap(self):
        for route in ('198.18.0.0/15', '198.18.0.128/25', '198.18.0.15/32'):
            with self.subTest(route=route), self.assertRaisesRegex(RuntimeError, 'overlaps'):
                provisioning.check_management_overlap(fixture(), [], [{'dst': route}])

    def test_local_interface_overlap_is_rejected(self):
        addresses = [{'addr_info': [{'family': 'inet', 'local': '198.18.0.100', 'prefixlen': 24}]}]
        with self.assertRaisesRegex(RuntimeError, 'overlaps'):
            provisioning.check_management_overlap(fixture(), addresses, [])

    def test_true_smt_pairs_and_reserved_emulators(self):
        result = provisioning.check_topology(fixture(), *host_topology())
        self.assertEqual(result, {'gpu-vm1': {0: [0, 1, 2, 3]}, 'gpu-vm2': {1: [0, 1, 2, 3]}})

    def test_cpu_order_must_match_virtual_smt_pairing(self):
        config = fixture()
        config['guests'][0]['cpu_pins'] = [0, 1, 8, 9]
        with self.assertRaisesRegex(RuntimeError, 'SMT core'):
            provisioning.check_topology(config, *host_topology())

    def test_offline_cpu_and_wrong_numa_are_rejected(self):
        for change in ('offline', 'numa'):
            config = fixture()
            topology, memory, available = host_topology()
            if change == 'offline':
                del topology[0]
            else:
                config['guests'][0]['numa_nodes'] = [1]
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                provisioning.check_topology(config, topology, memory, available)

    def test_node_free_memory_and_global_reserve(self):
        topology, memory, available = host_topology()
        memory[0]['free_mib'] = 32768
        with self.assertRaisesRegex(RuntimeError, 'NUMA node 0'):
            provisioning.check_topology(fixture(), topology, memory, available)
        topology, memory, _ = host_topology()
        with self.assertRaisesRegex(RuntimeError, 'Host available RAM'):
            provisioning.check_topology(fixture(), topology, memory, 70000)

    def test_emulator_cannot_use_other_guest_cpu(self):
        config = fixture()
        config['guests'][0]['emulator_cpus'] = [4]
        with self.assertRaisesRegex(RuntimeError, 'overlap another guest'):
            provisioning.check_topology(config, *host_topology())

    def test_vnic_identity_includes_nic_index_tag_address_subnet(self):
        config = fixture()
        metadata, addresses, links = host_network(config)
        provisioning.check_vnics(config, metadata, addresses, links)
        for field, value in [('nicIndex', 0), ('vlanTag', 200), ('privateIp', '192.0.2.99'),
                             ('macAddr', '02:00:00:00:ff:01'), ('subnetCidrBlock', '192.0.2.0/25')]:
            changed = copy.deepcopy(metadata)
            changed[1][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'metadata identity'):
                provisioning.check_vnics(config, changed, addresses, links)

    def test_existing_tag_on_other_link_is_rejected(self):
        config = fixture()
        metadata, addresses, links = host_network(config)
        links.append({'ifname': 'other', 'ifindex': 8, 'link_index': 2,
                      'linkinfo': {'info_kind': 'vlan', 'info_data': {'id': 100}}})
        with self.assertRaisesRegex(RuntimeError, 'parent/tag'):
            provisioning.check_vnics(config, metadata, addresses, links)

    def test_cpu_only_domain_has_memory_window_and_restart_policy(self):
        config = fixture()
        guest = config['guests'][0]
        xml = ET.fromstring(provisioning.render_domain(config, guest, {0: [0, 1, 2, 3]}, '/usr/bin/qemu-system-x86_64'))
        self.assertEqual(xml.findtext('on_reboot'), 'restart')
        self.assertEqual(xml.find('./devices/memballoon').get('model'), 'none')
        self.assertIsNone(xml.find('./devices/hostdev'))
        self.assertEqual(xml.find('./cpu/feature').get('name'), 'topoext')
        self.assertEqual(xml.find('./os/loader').get('secure'), 'no')
        self.assertEqual(xml.find('./numatune/memnode').get('nodeset'), '0')
        args = [v.get('value') for v in xml.findall(f'./{{{provisioning.QEMU_NS}}}commandline/*')]
        self.assertIn('q35-pcihost.pci-hole64-size=512G', args)
        self.assertIn('name=opt/ovmf/X-PciMmio64Mb,string=524288', args)
        self.assertEqual(len(xml.findall('./devices/interface')), 2)
        self.assertEqual(xml.find('./devices/interface/source').get('mode'), 'passthrough')

    def test_management_bridge_has_no_forwarding_or_dhcp(self):
        xml = ET.fromstring(provisioning.render_management(fixture()))
        self.assertIsNone(xml.find('forward'))
        self.assertIsNone(xml.find('./ip/dhcp'))
        self.assertEqual(xml.find('dns').get('enable'), 'no')

    def test_cloud_init_has_only_vnic_default_route(self):
        config = fixture()
        files = provisioning.render_cloud_init(config, config['guests'][0], 'ssh-ed25519 synthetic-key')
        network = json.loads(files['network-config'])['ethernets']
        self.assertNotIn('routes', network['mgmt0'])
        self.assertEqual(network['vcn0']['routes'], [{'to': '0.0.0.0/0', 'via': '192.0.2.1'}])
        self.assertFalse(network['vcn0']['dhcp4'])
        userdata = json.loads(files['user-data'].removeprefix('#cloud-config\n'))
        self.assertFalse(userdata['ssh_pwauth'])
        self.assertTrue(userdata['users'][0]['lock_passwd'])

    def test_streaming_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image'
            payload = b'image block' * 1000000
            path.write_bytes(payload)
            self.assertEqual(provisioning.stream_sha256(path), hashlib.sha256(payload).hexdigest())


if __name__ == '__main__':
    unittest.main()
