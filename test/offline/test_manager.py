import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock
import xml.etree.ElementTree as ET

from test_config import fixture, ROOT


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


manager = module('gpu_vm_manager', ROOT / 'files/host/gpu-vm-manager.py')
installer = module('install_services', ROOT / 'files/host/install-services.py')


def domain_xml(bdfs=(), managed='no'):
    root = ET.fromstring('<domain><uuid>test-domain</uuid><os><type machine="q35">hvm</type></os><on_reboot>destroy</on_reboot><devices><controller type="pci" index="0" model="pcie-root"/></devices></domain>')
    for bdf in bdfs:
        dom, bus, device = bdf.split(':')
        slot, func = device.split('.')
        dev = ET.SubElement(root.find('devices'), 'hostdev', type='pci', managed=managed)
        source = ET.SubElement(dev, 'source')
        ET.SubElement(source, 'address', domain='0x'+dom, bus='0x'+bus, slot='0x'+slot, function='0x'+func)
    return ET.tostring(root, encoding='unicode')


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.config = fixture()
        manager.configure(self.config)
        self.names = list(manager.VMS) + ['unrelated']

    def fake_run(self, args, **kwargs):
        if args[1:4] == ['list', '--all', '--name']:
            out = '\n'.join(self.names)
        elif args[1] == 'dominfo':
            out = 'Persistent: yes\n'
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, out, '')

    def test_unrelated_cpu_domain_allowed_without_fabric_vm(self):
        with patch.object(manager, 'run', side_effect=self.fake_run), patch.object(manager, 'domain_state', return_value='shut off'), patch.object(manager, 'xml', return_value=domain_xml()):
            manager.domain_checks(initial=True)

    def test_unrelated_domain_cannot_own_target_gpu_in_live_or_inactive_xml(self):
        for conflict_inactive in (False, True):
            def xml(name, inactive):
                return domain_xml([next(iter(manager.ALL_GPUS))], 'yes') if name == 'unrelated' and inactive == conflict_inactive else domain_xml()
            with self.subTest(inactive=conflict_inactive), patch.object(manager, 'run', side_effect=self.fake_run), patch.object(manager, 'domain_state', return_value='running'), patch.object(manager, 'xml', side_effect=xml):
                with self.assertRaisesRegex(RuntimeError, 'another domain owns'):
                    manager.domain_checks(initial=True)

    def test_unrelated_domain_cannot_own_switch(self):
        def xml(name, inactive):
            return domain_xml([next(iter(manager.SWITCHES))]) if name == 'unrelated' else domain_xml()
        with patch.object(manager, 'run', side_effect=self.fake_run), patch.object(manager, 'domain_state', return_value='shut off'), patch.object(manager, 'xml', side_effect=xml):
            with self.assertRaisesRegex(RuntimeError, 'another domain owns'):
                manager.domain_checks(initial=True)

    def test_target_managed_yes_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'managed=no'):
            manager.hostdevs(ET.fromstring(domain_xml([next(iter(manager.ALL_GPUS))], 'yes')))

    def test_generated_gpu_xml_contains_only_configured_four(self):
        for name in manager.VMS:
            root = ET.fromstring(manager.make_gpu_xml(domain_xml(), name))
            self.assertEqual(manager.hostdevs(root), set(manager.EXPECTED[name]))
            self.assertEqual(root.findtext('on_reboot'), 'restart')
            self.assertEqual(len(root.findall('./devices/hostdev')), 4)

    def partitions(self):
        return [{'partitionId': 10+i, 'isActive': False, 'gpuInfo': [{'uuid': u} for u in manager.EXPECTED[name].values()]} for i, name in enumerate(manager.VMS)]

    def test_partition_resolution_uses_exact_uuid_set(self):
        manager.verify_partitions(self.partitions())
        self.config['guests'][0]['partition_id'] = 10
        manager.configure(self.config)
        manager.verify_partitions(self.partitions())

    def test_partition_ambiguity_wrong_id_or_active_rejected(self):
        partitions = self.partitions()
        with self.assertRaisesRegex(RuntimeError, 'one inactive'):
            manager.verify_partitions(partitions + [partitions[0]])
        partitions[0]['isActive'] = True
        with self.assertRaisesRegex(RuntimeError, 'all inactive'):
            manager.verify_partitions(partitions)
        self.config['guests'][0]['partition_id'] = 99
        manager.configure(self.config)
        with self.assertRaisesRegex(RuntimeError, 'one inactive'):
            manager.verify_partitions(self.partitions())

    def test_provisioned_mmio_arguments_accepted_and_preserved(self):
        provisioning = module('provisioning_for_manager_test', ROOT / 'files/host/create-guests.py')
        guest = self.config['guests'][0]
        # Rendering only; no host, OCI, filesystem or subprocess work.
        cells = {node: list(range(i * 14, (i + 1) * 14))
                 for i, node in enumerate(guest['numa_nodes'])}
        cpu_xml = provisioning.render_domain(self.config, guest, cells, '/usr/bin/qemu-system-x86_64')
        manager.guest_qemu_args(ET.fromstring(cpu_xml))
        gpu_xml = manager.make_gpu_xml(cpu_xml, guest['name'])
        manager.guest_qemu_args(ET.fromstring(gpu_xml))
        self.assertEqual(manager.hostdevs(ET.fromstring(gpu_xml)), set(manager.EXPECTED[guest['name']]))
        poisoned = ET.fromstring(cpu_xml)
        ET.SubElement(poisoned.find('./{http://libvirt.org/schemas/domain/qemu/1.0}commandline'),
                      '{http://libvirt.org/schemas/domain/qemu/1.0}arg', value='-device')
        with self.assertRaisesRegex(RuntimeError, 'MMIO arguments'):
            manager.guest_qemu_args(poisoned)

    def test_config_drift_rejected_before_backup_reads(self):
        with tempfile.TemporaryDirectory() as d:
            manager.MANIFEST = Path(d) / 'manifest.json'
            manager.MANIFEST.write_text(json.dumps({'version':1, 'expected':manager.EXPECTED, 'configHash':'different'}))
            with patch.object(manager, 'trusted_path'):
                with self.assertRaisesRegex(RuntimeError, 'configuration changed'):
                    manager.load()

    def test_prepare_requires_attestation_before_any_host_call(self):
        with patch.object(manager, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'explicit attestation'):
                manager.prepare(False)
            run.assert_not_called()

    def test_live_uncertain_pid_blocks_mutation(self):
        with tempfile.TemporaryDirectory() as d:
            manager.STATE = Path(d)
            (manager.STATE / 'uncertain-command.json').write_text(json.dumps({'bootId':'same', 'pid':123, 'startTicks':'1234'}))
            with patch.object(manager, 'boot_id', return_value='same'), patch.object(manager, 'process_stamp', return_value='1234'):
                with self.assertRaisesRegex(RuntimeError, 'still exists'):
                    manager.no_uncertain_process()

    def test_timeout_marks_uncertainty_and_never_claims_cancellation(self):
        process = Mock(pid=123)
        process.communicate.side_effect = [subprocess.TimeoutExpired('virsh', 1), ('', '')]
        with patch.object(manager.subprocess, 'Popen', return_value=process), patch.object(manager, 'process_stamp', return_value='1'), patch.object(manager, 'uncertain_command') as mark:
            with self.assertRaisesRegex(manager.UncertainTransition, 'may still be in flight'):
                manager.run(['virsh', 'start', 'example'], timeout=1)
            mark.assert_called_once()
            process.kill.assert_called_once()

    def test_interrupted_restart_running_target_is_not_restarted(self):
        name = manager.VMS[0]
        saved = {'bootId':'same', 'phase':'restarting-' + name}
        run = Mock(return_value=subprocess.CompletedProcess([], 0, 'Job type: None\n', ''))
        with patch.object(manager, 'boot_id', return_value='same'), patch.object(manager, 'no_uncertain_process'), patch.object(manager, 'verify_running'), patch.object(manager, 'run', run), patch.object(manager, 'domain_state', return_value='running'):
            with self.assertRaisesRegex(RuntimeError, 'already running'):
                manager.restart(saved, name)
            self.assertTrue(all('start' not in call.args[0] for call in run.call_args_list))

    def test_service_has_no_fabric_manager_order_cycle(self):
        unit = installer.unit_text(self.config)
        self.assertNotIn('After=nvidia', unit)
        self.assertNotIn('nvidia-fabricmanager', unit)
        self.assertIn('stop --no-restore-guests', unit)
        self.assertIn('oci-kvm-net-a100-vm01.service', unit)

    def test_libvirt_guests_policy_replaces_active_values(self):
        text = installer.policy_text('ON_BOOT=start\nON_SHUTDOWN=suspend\nOTHER=keep\n')
        self.assertIn('ON_BOOT=ignore', text)
        self.assertIn('ON_SHUTDOWN=shutdown', text)
        self.assertIn('OTHER=keep', text)
        self.assertNotIn('ON_BOOT=start', text)


if __name__ == '__main__':
    unittest.main()
