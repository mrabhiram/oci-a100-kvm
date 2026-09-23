#!/usr/bin/env python3
"""Configuration-driven stock VFIO lifecycle for two NVLink-disabled A100 guests.

Install with files/host/install-services.py after reviewing deployment.json.
Initial invocation: prepare --guests-nvlink-disabled (an operator attestation
that BOTH guest initramfs files were prepared/verified before GPU assignment).
Then: start, inspect, restart a100-vm01, restart a100-vm02, stop.
stop --no-restore-guests is for systemd/host shutdown. Plain stop restores the
original CPU-only running state. There is no force-destroy, switch rebind,
reset_method write, reset suppression, or automatic rollback after failure.

Guest changes, direct virsh starts and uncoordinated GPU users are prohibited
while this manager owns the devices. It cannot verify guest CUDA/NCCL or a
guest's effective NvLinkDisable setting; those are separate acceptance checks.
"""
from datetime import datetime, timezone
from pathlib import Path
import argparse
import csv
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from config import config_hash, root_config, trusted_path

CONFIG = None
CONFIG_HASH = None
BASE = STATE = MANIFEST = CFG = CLIENT = None
PCI = Path('/sys/bus/pci/devices')
VMS = ()
SWITCHES = set()
EXPECTED = {}
ALL_GPUS = {}


def configure(config):
    global CONFIG, CONFIG_HASH, BASE, STATE, MANIFEST, CFG, CLIENT, VMS, SWITCHES, EXPECTED, ALL_GPUS
    CONFIG = config
    CONFIG_HASH = config_hash(config)
    BASE = Path(config['paths']['base_dir'])
    STATE = Path(config['paths']['state_dir'])
    MANIFEST = STATE / 'manifest.json'
    CFG = Path(config['paths']['fm_config'])
    CLIENT = Path(config['paths']['fm_client'])
    VMS = tuple(g['name'] for g in config['guests'])
    SWITCHES = set(config['switches'])
    EXPECTED = {g['name']: {gpu['bdf']: gpu['uuid'] for gpu in g['gpus']} for g in config['guests']}
    ALL_GPUS = {b: u for rows in EXPECTED.values() for b, u in rows.items()}


CLIENT_SERVICES = ('nvidia-persistenced.service', 'nvidia-dcgm.service',
                   'dcgm.service', 'dcgm-exporter.service')
FM = 'nvidia-fabricmanager.service'
ET.register_namespace('qemu', 'http://libvirt.org/schemas/domain/qemu/1.0')
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/sbin:/usr/local/bin',
       'LC_ALL': 'C', 'LANG': 'C', 'LIBVIRT_DEFAULT_URI': 'qemu:///system'}


class UncertainTransition(RuntimeError):
    """A timeout/interruption is not evidence that the operation stopped."""


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def log(message):
    print(datetime.now(timezone.utc).isoformat(), message, flush=True)


def process_stamp(pid):
    try:
        # Field 22, allowing spaces and parentheses in the comm field.
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def uncertain_command(p, args, stamp):
    if MANIFEST.exists():
        atomic_json(STATE / 'uncertain-command.json', {
            'bootId': boot_id(), 'pid': p.pid, 'startTicks': stamp,
            'args': args, 'timestampUtc': datetime.now(timezone.utc).isoformat()})


def no_uncertain_process():
    marker = STATE / 'uncertain-command.json'
    if not marker.exists():
        return
    record = json.loads(marker.read_text())
    if record['bootId'] == boot_id():
        current = process_stamp(record['pid'])
        require(current is None or current != record['startTicks'],
                f'Uncertain prior command PID {record["pid"]} still exists; no new mutations')
    log('Prior uncertain command process no longer exists (or host rebooted); independent ownership checks still required.')


def run(args, *, check=True, show=True, timeout=120):
    args = list(map(str, args))
    log('+ ' + ' '.join(args))
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, env=ENV)
    stamp = process_stamp(p.pid)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        uncertain_command(p, args, stamp)
        p.kill()
        try:
            out, err = p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            raise UncertainTransition(f'PID {p.pid} remained blocked after kill: {args[0]}')
        if out:
            print(out, flush=True)
        if err:
            print(err, flush=True)
        raise UncertainTransition(f'{args[0]} timed out; kernel/libvirt operation may still be in flight')
    except UncertainTransition:
        uncertain_command(p, args, stamp)
        raise
    if show and out:
        print(out, flush=True)
    if err:
        print(err, flush=True)
    require(not check or p.returncode == 0,
            f'Command failed ({p.returncode}): {args[0]}: {err[-2000:]}')
    return subprocess.CompletedProcess(args, p.returncode, out, err)


def write_sysfs(path, value):
    # A separate process bounds potentially blocking PCI writes. Never assume
    # killing that process cancels a kernel operation or permits a rebind.
    run(['python3', '-c', 'from pathlib import Path; import sys; '
         'Path(sys.argv[1]).write_text(sys.argv[2])', path, value],
        show=False, timeout=90)


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def atomic_json(path, data):
    trusted_path(path, allow_missing=True)
    tmp = path.with_name(path.name + '.tmp')
    trusted_path(tmp, allow_missing=True)
    with tmp.open('w') as f:
        os.chmod(tmp, 0o600)
        json.dump(data, f, indent=2)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def checkpoint(saved, phase):
    saved.update(phase=phase, bootId=boot_id(),
                 updatedUtc=datetime.now(timezone.utc).isoformat())
    atomic_json(MANIFEST, saved)
    log('Lifecycle phase: ' + phase)


def mode(text):
    values = re.findall(r'^FABRIC_MODE=(\d+)\s*$', text, re.M)
    require(len(values) == 1, 'Ambiguous FABRIC_MODE')
    return int(values[0])


def active(unit):
    p = run(['systemctl', 'is-active', unit], check=False, show=False)
    return p.stdout.strip() == 'active'


def domain_state(name):
    return run(['virsh', 'domstate', name], show=False, timeout=120).stdout.strip()


def xml(name, inactive=False):
    return run(['virsh', 'dumpxml', name] + (['--inactive'] if inactive else []),
               show=False, timeout=120).stdout


def hostdevs(root):
    devices = root.findall('./devices/hostdev')
    result = []
    for dev in devices:
        require(dev.get('type') == 'pci', 'Non-PCI hostdev encountered')
        address = dev.find('./source/address')
        require(address is not None, 'hostdev has no source address')
        result.append('%04x:%02x:%02x.%x' % tuple(
            int(address.get(key), 0) for key in ('domain', 'bus', 'slot', 'function')))
        require(dev.get('managed') == 'no', 'Only explicitly managed=no PCI devices are accepted')
    require(len(result) == len(set(result)), 'Duplicate hostdev')
    return set(result)


def pci_sources(root):
    """Read PCI sources even from unrelated domains with managed=yes devices."""
    result = set()
    for dev in root.findall("./devices/hostdev[@type='pci']"):
        address = dev.find('./source/address')
        require(address is not None, 'PCI hostdev has no source address')
        result.add('%04x:%02x:%02x.%x' % tuple(
            int(address.get(key), 0) for key in ('domain', 'bus', 'slot', 'function')))
    # A hostdev-backed interface can also own a PCI endpoint.
    for address in root.findall("./devices/interface[@type='hostdev']/source/address"):
        require(address.get('type', 'pci') == 'pci', 'Unknown hostdev interface source')
        result.add('%04x:%02x:%02x.%x' % tuple(
            int(address.get(key), 0) for key in ('domain', 'bus', 'slot', 'function')))
    return result


def guest_qemu_args(root):
    """Only the reviewed large PCI MMIO window may use QEMU escape arguments."""
    namespace = '{http://libvirt.org/schemas/domain/qemu/1.0}'
    commandlines = root.findall('./' + namespace + 'commandline')
    if not commandlines:
        return
    require(len(commandlines) == 1, 'Multiple custom QEMU command lines')
    children = list(commandlines[0])
    require(all(child.tag == namespace + 'arg' and set(child.attrib) == {'value'} for child in children),
            'Custom QEMU environment or unknown elements are not supported')
    values = [child.get('value') for child in children]
    require(values == ['-fw_cfg', 'name=opt/ovmf/X-PciMmio64Mb,string=524288',
                       '-global', 'q35-pcihost.pci-hole64-size=512G'],
            'Only the reviewed 512 GiB Q35/OVMF MMIO arguments are accepted')


def domain_checks(saved=None, *, initial=False):
    names = set(run(['virsh', 'list', '--all', '--name'], show=False).stdout.split())
    require(set(VMS).issubset(names), 'A configured guest domain is missing')
    protected = set(ALL_GPUS) | SWITCHES
    for name in sorted(names):
        state = domain_state(name)
        if name in VMS:
            require(state in ('running', 'shut off'), f'{name}: unexpected state {state}')
        persistent = run(['virsh', 'dominfo', name], show=False).stdout
        is_persistent = re.search(r'^Persistent:\s+yes\s*$', persistent, re.M) is not None
        require(name not in VMS or is_persistent, f'{name}: persistent definition required')
        variants = ([True] if is_persistent else []) + ([False] if state != 'shut off' else [])
        require(variants, f'{name}: cannot inspect definition')
        for inactive in variants:
            root = ET.fromstring(xml(name, inactive))
            if name not in VMS:
                require(not pci_sources(root).intersection(protected),
                        f'{name}: another domain owns a configured GPU or NVSwitch')
                # Unparsed custom QEMU arguments could hide device ownership.
                require(not root.findall('.//{http://libvirt.org/schemas/domain/qemu/1.0}commandline'),
                        f'{name}: custom QEMU command line requires manual ownership review')
                continue
            devices = hostdevs(root)
            require(not devices if initial else devices in (set(), set(EXPECTED[name])),
                    f'{name}: unexpected PCI assignment {devices}')
            require(pci_sources(root) == devices, f'{name}: unexpected interface PCI assignment')
            guest_qemu_args(root)
            if saved:
                original = ET.fromstring((Path(saved['backup']) / f'{name}.cpu.xml').read_text())
                require(root.findtext('uuid') == original.findtext('uuid'), f'{name}: domain UUID changed')
                disks = [p.get('file') for p in root.findall('./devices/disk/source')]
                old_disks = [p.get('file') for p in original.findall('./devices/disk/source')]
                require(disks == old_disks, f'{name}: storage paths changed')


def pci(bdf, expected=None):
    p = PCI / bdf
    require((p / 'vendor').read_text().strip() == '0x10de', f'{bdf}: wrong vendor')
    require((p / 'device').read_text().strip() == ('0x1af1' if bdf in SWITCHES else '0x20b0'),
            f'{bdf}: wrong device')
    driver = (p / 'driver').resolve().name if (p / 'driver').exists() else None
    if expected is not None:
        require(driver == expected, f'{bdf}: driver {driver}, expected {expected}')
    with (p / 'config').open('rb') as f:
        header = f.read(64)
    require(len(header) == 64 and header[:2] == bytes.fromhex('de10') and header[61] in range(5),
            f'{bdf}: inaccessible/invalid PCI configuration')
    if bdf in ALL_GPUS:
        require((p / 'iommu_group').exists(), f'{bdf}: missing IOMMU group')
        require({q.name for q in (p / 'iommu_group/devices').iterdir()} == {bdf},
                f'{bdf}: nonexclusive IOMMU group')
        peers = {q.name for q in p.resolve().parent.iterdir()
                 if re.fullmatch(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]', q.name)}
        require(peers == {bdf}, f'{bdf}: reset bus has another PCI endpoint')
    return p, driver


def switches():
    for bdf in sorted(SWITCHES):
        pci(bdf, 'nvidia-nvswitch')


def gpu_identities():
    output = run(['nvidia-smi', '--query-gpu=pci.bus_id,uuid', '--format=csv,noheader']).stdout
    rows = {}
    for bdf, gpu_uuid in csv.reader(io.StringIO(output)):
        domain, bus, dev = bdf.strip().lower().split(':')
        rows[f'{int(domain, 16):04x}:{bus}:{dev}'] = gpu_uuid.strip()
    require(rows == ALL_GPUS, 'Exact eight GPU BDF/UUID identity set differs')


def audit():
    rows = {}
    for line in run(['nvswitch-audit', '-f']).stdout.splitlines():
        fields = line.split()
        if len(fields) == 17 and fields[0].isdigit():
            rows[int(fields[0])] = fields[1:]
    require(set(range(1, 9)).issubset(rows), 'Unrecognized NVSwitch route matrix')
    for i in range(1, 9):
        require(rows[i][i-1] == 'X' and all(v == '0' for j, v in enumerate(rows[i], 1) if j != i),
                f'Nonzero/unrecognized route for physical GPU {i}: {rows[i]}')
    log('No off-diagonal routes from physical GPUs 1–8 (observed at this checkpoint).')


def no_gpu_apps():
    require(not run(['nvidia-smi', '--query-compute-apps=pid', '--format=csv,noheader']).stdout.strip(),
            'Host GPU workloads are present; no process will be killed')


def no_holders(paths):
    paths = [str(p) for p in paths if Path(p).exists()]
    if not paths:
        return
    result = run(['fuser', *sorted(set(paths))], check=False)
    require(result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip(),
            'Device users or fuser inspection error; ownership is not clear')


def no_guest_owners(names):
    no_uncertain_process()
    for name in names:
        require(domain_state(name) == 'shut off', f'{name} is not shut off')
        result = run(['virsh', 'domjobinfo', name], check=False, timeout=120)
        require((result.returncode == 0 and re.search(r'^Job type:\s+None\s*$', result.stdout, re.M))
                or (result.returncode == 1 and 'domain is not running' in result.stderr
                    and domain_state(name) == 'shut off'), f'{name}: unresolved libvirt job')
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            args = (proc / 'cmdline').read_bytes().split(b'\0')
        except (FileNotFoundError, ProcessLookupError):
            continue
        if args and b'qemu-system' in args[0]:
            for name in names:
                encoded = name.encode()
                require(not any(a.split(b',', 1)[0] == b'guest=' + encoded or a == encoded for a in args),
                        f'{name}: QEMU PID {proc.name} remains')
    bdfs = {bdf for name in names for bdf in EXPECTED[name]}
    groups = {(PCI / bdf / 'iommu_group').resolve().name for bdf in bdfs}
    no_holders([Path('/dev/vfio') / group for group in groups])
    # Also cover cdev/iommufd VFIO ownership, should an external tool have used it.
    for bdf in bdfs:
        no_holders([Path('/dev/vfio/devices') / p.name
                    for p in (PCI / bdf / 'vfio-dev').glob('vfio*')])


def shutdown(name):
    if domain_state(name) == 'running':
        run(['virsh', 'shutdown', name], timeout=30)
    deadline = time.monotonic() + 240
    while domain_state(name) != 'shut off':
        require(time.monotonic() < deadline, f'{name}: graceful shutdown deadline; no force destroy/rebind')
        time.sleep(3)
    no_guest_owners([name])


def make_gpu_xml(cpu_xml, name):
    root = ET.fromstring(cpu_xml)
    require(not hostdevs(root), 'CPU backup unexpectedly has hostdevs')
    devices = root.find('devices')
    indexes = [int(c.get('index')) for c in devices.findall("controller[@type='pci']")]
    require(indexes, 'A Q35 PCI controller is required')
    for index in range(max(indexes) + 1, max(indexes) + 5):
        ET.SubElement(devices, 'controller', type='pci', index=str(index), model='pcie-root-port')
    for bdf in EXPECTED[name]:
        domain, bus, slot_fn = bdf.split(':')
        slot, function = slot_fn.split('.')
        dev = ET.SubElement(devices, 'hostdev', mode='subsystem', type='pci', managed='no')
        ET.SubElement(dev, 'driver', name='vfio')
        source = ET.SubElement(dev, 'source')
        ET.SubElement(source, 'address', domain='0x' + domain, bus='0x' + bus,
                      slot='0x' + slot, function='0x' + function)
        ET.SubElement(dev, 'rom', bar='off')
    # Stock VFIO resets are allowed with NVLink disabled. Guest-initiated
    # reboot is enabled here but requires its own live acceptance test.
    reboot = root.find('on_reboot')
    if reboot is None:
        reboot = ET.SubElement(root, 'on_reboot')
    reboot.text = 'restart'
    ET.indent(root)
    return '<!-- NVLink-disabled PCIe-only PoC; stock VFIO resets -->\n' + ET.tostring(root, encoding='unicode') + '\n'


def host_scope():
    import ipaddress
    require(os.geteuid() == 0, 'Run as root')
    expected = ipaddress.ip_interface(CONFIG['host']['primary_cidr'])
    interfaces = json.loads(run(['ip', '-j', 'address', 'show', CONFIG['host']['interface']], show=False).stdout)
    require(any(a.get('local') == str(expected.ip) and a.get('prefixlen') == expected.network.prefixlen
                for interface in interfaces for a in interface.get('addr_info', [])),
            'Wrong host private IP, prefix or interface')
    log('Scope: configured host interface and exact eight approved A100 BDF/UUID identities')


def load():
    trusted_path(MANIFEST)
    saved = json.loads(MANIFEST.read_text())
    require(saved['version'] == 1 and saved['expected'] == EXPECTED and saved['configHash'] == CONFIG_HASH,
            'Deployment manifest or configuration changed; restore the prepared configuration')
    backup = Path(saved['backup'])
    require(backup.parent == STATE / 'backups' and backup.is_dir(), 'Invalid backup path')
    trusted_path(backup, directory=True)
    trusted_path(backup / 'original-state.json')
    original = json.loads((backup / 'original-state.json').read_text())
    for key in ('configHash', 'expected', 'devices', 'services', 'vmWasRunning', 'backup',
                'backupHashes', 'guestNvLinkDisabledAttested'):
        require(original[key] == saved[key], f'Manifest recovery baseline differs: {key}')
    for name, digest in saved['backupHashes'].items():
        trusted_path(backup / name)
        require(Path(name).name == name and hashlib.sha256((backup / name).read_bytes()).hexdigest() == digest,
                f'Original backup changed: {name}')
    return saved


def prepare(attested):
    require(attested, 'Both guests need verified NvLinkDisable=1 in their boot initramfs; pass explicit attestation')
    require(not MANIFEST.exists(), 'Deployment already prepared; backups will not be replaced')
    domain_checks(initial=True)
    original = CFG.read_text()
    require(mode(original) == 0 and re.search(r'^FABRIC_MODE_RESTART=0\s*$', original, re.M),
            'Prepare requires original FM mode0 and FABRIC_MODE_RESTART=0')
    require(active(FM), 'Prepare requires active, healthy host Fabric Manager')
    switches()
    for bdf in ALL_GPUS:
        pci(bdf, 'nvidia')
    gpu_identities()
    no_gpu_apps()
    devices = []
    for bdf, gpu_uuid in ALL_GPUS.items():
        p, _ = pci(bdf, 'nvidia')
        require((p / 'reset_method').read_text().split() == ['flr', 'bus'],
                f'{bdf}: expected stock flr bus reset methods; no reset policy will be changed')
        override = (p / 'driver_override').read_text().strip()
        require(override in ('', '(null)'), f'{bdf}: existing driver override {override}')
        devices.append({'bdf': bdf, 'uuid': gpu_uuid, 'driverOverride': '',
                        'powerControl': (p / 'power/control').read_text().strip(),
                        'resetMethod': (p / 'reset_method').read_text().strip()})
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = STATE / 'backups' / (stamp + '-' + uuid.uuid4().hex[:8])
    trusted_path(STATE / 'backups', allow_missing=True, directory=True)
    trusted_path(backup, allow_missing=True, directory=True)
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(CFG, backup / 'fabricmanager.cfg.mode0')
    saved = {'version': 1, 'configHash': CONFIG_HASH, 'expected': EXPECTED, 'backup': str(backup),
             'devices': devices, 'guestNvLinkDisabledAttested': True,
             'vmWasRunning': {name: domain_state(name) == 'running' for name in VMS},
             'services': {unit: active(unit) for unit in (FM, *CLIENT_SERVICES)},
             'createdUtc': datetime.now(timezone.utc).isoformat()}
    for name in sorted(VMS):
        cpu_xml = xml(name, True)
        (backup / f'{name}.cpu.xml').write_text(cpu_xml)
        if name in VMS:
            (backup / f'{name}.gpu.xml').write_text(make_gpu_xml(cpu_xml, name))
            run(['virt-xml-validate', backup / f'{name}.gpu.xml', 'domain'])
    saved['backupHashes'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in backup.iterdir()}
    atomic_json(backup / 'original-state.json', saved)
    for p in backup.iterdir():
        p.chmod(0o400)
    checkpoint(saved, 'prepared')
    for name in sorted(VMS):
        run(['virsh', 'autostart', '--disable', name])
    log('Prepared immutable CPU/GPU XML, original config/device/service snapshots. No GPUs transferred.')


def stop_client_services():
    for unit in CLIENT_SERVICES:
        if active(unit):
            run(['systemctl', 'stop', unit], timeout=60)


def restore_configuration(saved, restore_guests):
    backup = Path(saved['backup'])
    shutting_down = run(['systemctl', 'is-system-running'], check=False, show=False).stdout.strip() == 'stopping'
    if shutting_down:
        # This unit stops before FM/client services. Waiting on their queued
        # stop/start jobs here would deadlock against systemd's ordering graph.
        require(not active(FM), 'Host shutdown found unexpectedly active FM; no rebind or service job')
        require(not any(active(unit) for unit in CLIENT_SERVICES),
                'Host shutdown found unexpectedly active GPU client service; no rebind')
        restore_guests = False
    else:
        if any(pci(bdf)[1] == 'nvidia' for bdf in ALL_GPUS):
            no_gpu_apps()
        run(['systemctl', 'stop', FM], timeout=120)
    for row in saved['devices']:
        bdf = row['bdf']
        p, driver = pci(bdf)
        require(driver in (None, 'vfio-pci', 'nvidia'), f'{bdf}: unexpected owner {driver}')
        require((p / 'reset_method').read_text().strip() == row['resetMethod'], f'{bdf}: reset policy changed')
        if driver == 'vfio-pci':
            write_sysfs(p / 'driver/unbind', bdf + '\n')
        write_sysfs(p / 'driver_override', row['driverOverride'] + '\n')
        if driver != 'nvidia':
            write_sysfs(Path('/sys/bus/pci/drivers_probe'), bdf + '\n')
        pci(bdf, 'nvidia')
        write_sysfs(p / 'power/control', row['powerControl'] + '\n')
    switches()
    # Preserve the installed config's ownership/mode; backups are read-only.
    shutil.copyfile(backup / 'fabricmanager.cfg.mode0', CFG)
    require(mode(CFG.read_text()) == 0, 'Original FM configuration not restored')
    for name in VMS:
        run(['virsh', 'define', '--validate', backup / f'{name}.cpu.xml'])
        run(['virsh', 'autostart', '--disable', name])
    if not shutting_down:
        for unit, was_active in saved['services'].items():
            if was_active:
                run(['systemctl', 'start', unit], timeout=180)
            elif active(unit):
                run(['systemctl', 'stop', unit], timeout=120)
    gpu_identities()
    if restore_guests:
        for name in VMS:
            if saved['vmWasRunning'][name]:
                run(['virsh', 'start', name], timeout=300)
    checkpoint(saved, 'stopped')
    if shutting_down:
        log('Host shutdown: PCI ownership/CPU XML/FM mode0 config restored; service starts deferred to next boot.')
    else:
        log('Host driver/FM baseline restored. Fresh CUDA/NCCL/guest validation is a separate step.')


def stop(saved, restore_guests=True):
    domain_checks(saved)
    checkpoint(saved, 'stopping')
    for name in VMS:
        shutdown(name)
    no_guest_owners(VMS)
    switches()
    restore_configuration(saved, restore_guests)


def verify_running(saved, allow_stopped=()):
    domain_checks(saved)
    require(mode(CFG.read_text()) == 1 and not active(FM), 'Running deployment requires inactive host FM mode1')
    for unit in CLIENT_SERVICES:
        require(not active(unit), f'{unit}: unexpected active GPU client service')
    for name in VMS:
        allowed = ('running', 'shut off') if name in allow_stopped else ('running',)
        require(domain_state(name) in allowed, f'{name}: unexpected domain state')
        for inactive in (False, True):
            require(hostdevs(ET.fromstring(xml(name, inactive))) == set(EXPECTED[name]),
                    f'{name}: wrong GPU assignment')
    for row in saved['devices']:
        p, _ = pci(row['bdf'], 'vfio-pci')
        require((p / 'reset_method').read_text().strip() == row['resetMethod'], 'Reset policy changed')
    switches()
    audit()


def verify_partitions(partitions):
    require(partitions and not any(p['isActive'] for p in partitions), 'FM partitions are not all inactive')
    for guest in CONFIG['guests']:
        name, partition_id = guest['name'], guest['partition_id']
        matches = [p for p in partitions if
                   {gpu['uuid'] for gpu in p['gpuInfo']} == set(EXPECTED[name].values()) and
                   (partition_id is None or p['partitionId'] == partition_id)]
        require(len(matches) == 1, f'{name}: exact GPU set must identify one inactive FM partition')
        log(f'{name}: verified inactive FM partition {matches[0]["partitionId"]}; not activating it')


def start(saved):
    domain_checks(saved)
    no_uncertain_process()
    for unit in ('libvirtd.service', *(f'oci-kvm-net-{name}.service' for name in VMS)):
        require(active(unit), f'Required service is not active: {unit}')
    info = run(['virsh', 'net-info', CONFIG['management']['name']], show=False).stdout
    require(re.search(r'^Active:\s+yes\s*$', info, re.M), 'Configured isolated management network is not active')
    if saved['bootId'] == boot_id() and saved['phase'] == 'running':
        verify_running(saved)
        log('Both GPU domains already running with expected assignments; no changes.')
        return
    if saved['bootId'] != boot_id():
        # Previous process state is obsolete only after proving a fresh host
        # boot left every GPU with NVIDIA and no guest or VFIO owner survived.
        no_guest_owners(VMS)
        for bdf in ALL_GPUS:
            pci(bdf, 'nvidia')
        switches()
        checkpoint(saved, 'normalizing-new-boot')
        restore_configuration(saved, restore_guests=False)
    require(saved['phase'] in ('prepared', 'stopped'),
            f'Phase {saved["phase"]} needs inspect/explicit stop; no automatic rebind')
    for bdf in ALL_GPUS:
        pci(bdf, 'nvidia')
    switches()
    gpu_identities()
    no_gpu_apps()
    checkpoint(saved, 'starting')
    for name in VMS:
        shutdown(name)
    no_guest_owners(VMS)
    stop_client_services()
    run(['systemctl', 'stop', FM], timeout=120)
    original = (Path(saved['backup']) / 'fabricmanager.cfg.mode0').read_text()
    changed, count = re.subn(r'^FABRIC_MODE=0\s*$', 'FABRIC_MODE=1', original, flags=re.M)
    require(count == 1, 'Could not construct FM mode1 config')
    CFG.write_text(changed)
    run(['systemctl', 'start', FM], timeout=180)
    partitions = json.loads(run([CLIENT, '-l'], show=False).stdout)['partitionInfo']
    verify_partitions(partitions)
    audit()
    run(['systemctl', 'stop', FM], timeout=120)
    require(not active(FM), 'Fabric Manager did not stop')
    audit()
    no_holders([p for p in Path('/dev').glob('nvidia*') if p.is_char_device()])
    checkpoint(saved, 'binding-gpus')
    run(['modprobe', 'vfio-pci'])
    for row in saved['devices']:
        bdf = row['bdf']
        p, _ = pci(bdf, 'nvidia')
        write_sysfs(p / 'power/control', 'on\n')
        write_sysfs(p / 'driver_override', 'vfio-pci\n')
        write_sysfs(p / 'driver/unbind', bdf + '\n')
        write_sysfs(Path('/sys/bus/pci/drivers_probe'), bdf + '\n')
        pci(bdf, 'vfio-pci')
        require((p / 'reset_method').read_text().strip() == row['resetMethod'], f'{bdf}: reset policy changed')
    switches()
    checkpoint(saved, 'starting-guests')
    for name in VMS:
        run(['virsh', 'define', '--validate', Path(saved['backup']) / f'{name}.gpu.xml'])
        run(['virsh', 'autostart', '--disable', name])
        run(['virsh', 'start', name], timeout=360)
        require(domain_state(name) == 'running', f'{name}: launch not confirmed')
        audit()
    verify_running(saved)
    checkpoint(saved, 'running')
    log('Two four-GPU domains running. Guest NvLinkDisable, exact UUIDs, CUDA and NCCL acceptance still required.')


def restart(saved, name):
    phase = 'restarting-' + name
    require(saved['bootId'] == boot_id() and saved['phase'] in ('running', phase),
            'Restart requires running deployment or the exact same interrupted guest restart')
    no_uncertain_process()
    resuming = saved['phase'] == phase
    verify_running(saved, allow_stopped=(name,))
    for other in VMS:
        if other == name:
            continue
        job = run(['virsh', 'domjobinfo', other], timeout=120)
        require(re.search(r'^Job type:\s+None\s*$', job.stdout, re.M),
                f'{other}: other guest has an in-flight libvirt job')
    if resuming:
        # The previous operation could have timed out before shutdown completed
        # or after launch. Only the observed shut-off branch is resumed here.
        # A running target needs guest boot-identity review, not another shutdown
        # or an unsupported claim that the interrupted restart already completed.
        require(domain_state(name) == 'shut off',
                'Unfinished restart target is already running; inspect boot identity and reconcile explicitly')
        no_guest_owners([name])
        log('Resuming the exact interrupted restart after verified shutdown/ownership release: ' + name)
    else:
        checkpoint(saved, phase)
        shutdown(name)
    no_guest_owners([name])
    # NVIDIA devices stay VFIO-owned. Normal QEMU/VFIO reset behavior applies;
    # no Fabric Manager activation or PCI rebind is needed in this NVLink-off mode.
    switches()
    audit()
    run(['virsh', 'start', name], timeout=360)
    verify_running(saved)
    checkpoint(saved, 'running')
    log(name + ' restarted; the other domain was left running. Recheck guest CUDA/NCCL.')


def inspect(saved):
    log(json.dumps({key: saved[key] for key in ('phase', 'bootId', 'updatedUtc', 'backup')}))
    domain_checks(saved)
    for bdf in ALL_GPUS:
        p, driver = pci(bdf)
        log(f'{bdf}: driver={driver}; reset_method={(p / "reset_method").read_text().strip()}')
    switches()
    log('FM config mode=' + str(mode(CFG.read_text())) + '; active=' + str(active(FM)))
    if mode(CFG.read_text()) == 1:
        audit()
    else:
        run(['nvswitch-audit', '-f'])
        log('FM mode0 baseline may have active fabric routes; zero routing is required only in the PCIe-only deployment.')
    run(['journalctl', '-b', '-k', '--no-pager', '--grep=NVRM: Xid|SXid|RmInitAdapter|API mismatch'], check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', default='/etc/oci-a100-kvm/deployment.json')
    sub = parser.add_subparsers(dest='action', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--guests-nvlink-disabled', action='store_true')
    sub.add_parser('start')
    p = sub.add_parser('stop')
    p.add_argument('--no-restore-guests', action='store_true')
    p = sub.add_parser('restart')
    p.add_argument('guest')
    sub.add_parser('inspect')
    args = parser.parse_args()
    configure(root_config(args.config))
    if args.action == 'restart':
        require(args.guest in VMS, 'Guest is not configured')
    trusted_path(CFG)
    trusted_path(CLIENT)
    trusted_path(STATE, allow_missing=True, directory=True)
    host_scope()
    require(not STATE.is_symlink(), 'State path must not be a symlink')
    STATE.mkdir(mode=0o700, exist_ok=True)
    require(STATE.stat().st_uid == 0 and STATE.stat().st_mode & 0o077 == 0, 'State directory must be root-owned mode0700')
    for leaf in ('manager.lock', 'manifest.json', 'uncertain-command.json'):
        trusted_path(STATE / leaf, allow_missing=True)
    with (STATE / 'manager.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'prepare':
            prepare(args.guests_nvlink_disabled)
            return
        saved = load()
        if args.action == 'start':
            start(saved)
        elif args.action == 'stop':
            stop(saved, not args.no_restore_guests)
        elif args.action == 'restart':
            restart(saved, args.guest)
        else:
            inspect(saved)


def interrupted(signum, frame):
    raise UncertainTransition(f'Signal {signum}; command/kernel work may still be in flight')


if __name__ == '__main__':
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, interrupted)
    try:
        main()
    except BaseException as exc:
        log('STOPPED: ' + repr(exc))
        log('No automatic rollback performed. Inspect domain jobs, QEMU/VFIO owners, PCI and saved phase before explicit recovery.')
        raise
