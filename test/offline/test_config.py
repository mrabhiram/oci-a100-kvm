import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'files/lib'))
from config import ConfigError, config_hash, load_config, trusted_path, validate_config


def fixture():
    c = json.loads((ROOT / 'manifests/deployment.example.json').read_text())
    c['host']['interface'] = 'eth0'
    c['paths']['image_sha256'] = 'abcd' * 16
    c['switches'] = [f'0000:{n:02x}:00.0' for n in range(96, 102)]
    for index, g in enumerate(c['guests']):
        for n, gpu in enumerate(g['gpus']):
            gpu['bdf'] = f'0000:{32+index*4+n:02x}:00.0'
            gpu['uuid'] = 'GPU-' + str(uuid.uuid5(uuid.NAMESPACE_DNS, f'oci-a100-kvm-test-{index}-{n}'))
        g['vnic'].update(id=f'ocid1.vnic.oc1.example.synthetic{index}',
                         mac=f'02:00:00:00:01:0{index+1}', vlan_tag=100+index)
    return c


class ConfigTests(unittest.TestCase):
    def test_valid_example_filled(self):
        validate_config(fixture())

    def test_placeholder_example_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(ROOT / 'manifests/deployment.example.json')

    def test_hash_ignores_key_order(self):
        c = fixture()
        self.assertEqual(config_hash(c), config_hash(dict(reversed(list(c.items())))))

    def test_duplicate_json_keys_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'bad.json'
            path.write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaisesRegex(ConfigError, 'Duplicate JSON'):
                load_config(path)

    def test_mutated_config_rejected(self):
        changes = [
            lambda c: c.update(extra=True),
            lambda c: c['host'].update(mtu=True),
            lambda c: c['paths'].update(state_dir='/var/lib/../etc'),
            lambda c: c['paths'].update(fm_client='/tmp/tool;reboot'),
            lambda c: c['guests'][1].update(cpu_pins=c['guests'][0]['cpu_pins']),
            lambda c: c['guests'][1].update(emulator_cpus=c['guests'][0]['cpu_pins'][:2]),
            lambda c: c['guests'][1].update(emulator_cpus=c['guests'][0]['emulator_cpus']),
            lambda c: c['guests'][1].update(numa_nodes=c['guests'][0]['numa_nodes']),
            lambda c: c['guests'][1]['gpus'][0].update(bdf=c['guests'][0]['gpus'][0]['bdf']),
            lambda c: c['guests'][1]['gpus'][0].update(uuid=c['guests'][0]['gpus'][0]['uuid']),
            lambda c: c['guests'][0]['gpus'].pop(),
            lambda c: c['guests'][0]['management'].update(ip='10.99.0.1'),
            lambda c: c['guests'][0]['vnic'].update(gateway='192.0.2.1'),
            lambda c: c['guests'][1]['vnic'].update(vlan_tag=c['guests'][0]['vnic']['vlan_tag']),
            lambda c: c['guests'][1]['vnic'].update(mac=c['guests'][0]['vnic']['mac']),
            lambda c: c['guests'][0]['vnic'].update(nic_index=1),
            lambda c: c['guests'][0].update(name='vm;reboot'),
            lambda c: c['guests'][0].update(partition_id=True),
        ]
        for n, change in enumerate(changes):
            with self.subTest(change=n):
                c = fixture()
                change(c)
                with self.assertRaises(ConfigError):
                    validate_config(c)

    def test_own_or_reserved_emulator_cpus_allowed(self):
        c = fixture()
        validate_config(c)
        c['guests'][0]['emulator_cpus'] = c['guests'][0]['cpu_pins'][:2]
        validate_config(c)

    def test_null_or_explicit_partitions(self):
        c = fixture()
        validate_config(c)
        c['guests'][0]['partition_id'] = 5
        c['guests'][1]['partition_id'] = 6
        validate_config(c)
        c['guests'][1]['partition_id'] = 5
        with self.assertRaises(ConfigError):
            validate_config(c)

    def test_symlink_rejected_even_if_target_root_owned(self):
        import os
        with tempfile.TemporaryDirectory() as d:
            link = Path(d) / 'config'
            link.symlink_to('/etc/passwd')
            # Assert leaf symlink fails without relying on macOS /tmp ancestry.
            fake = type('S', (), {'st_mode': 0o40755, 'st_uid': 0})()
            real = Path.lstat
            with patch.object(Path, 'lstat', lambda p: real(p) if p == link else fake):
                with self.assertRaisesRegex(ConfigError, 'Symlink'):
                    trusted_path(link)


if __name__ == '__main__':
    unittest.main()
