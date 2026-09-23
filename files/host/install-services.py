#!/usr/bin/env python3
"""Install reviewed host helpers and an initially disabled lifecycle service.

This copies scripts and changes libvirt-guests boot/shutdown policy. It does
not start guests, transfer GPUs, prepare the lifecycle state or enable boot
startup. Run --execute only after reviewing the config and scripts.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from config import root_config, trusted_path

INSTALL_ROOT = Path('/usr/local/lib/oci-a100-kvm')
CONFIG_DEST = Path('/etc/oci-a100-kvm/deployment.json')
UNIT = Path('/etc/systemd/system/a100-pcie-two-vm.service')
LINK = Path('/usr/local/sbin/a100-pcie-two-vm')
POLICY = Path('/etc/default/libvirt-guests')


def run(args, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=True, timeout=120)


def unit_text(config):
    units = ' '.join(f'oci-kvm-net-{g["name"]}.service' for g in config['guests'])
    return f'''[Unit]
Description=Two A100 KVM guests with NVLink disabled
Requires=libvirtd.service libvirt-guests.service {units}
After=libvirtd.service libvirt-guests.service {units}

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/a100-pcie-two-vm --config /etc/oci-a100-kvm/deployment.json start
ExecStop=/usr/local/sbin/a100-pcie-two-vm --config /etc/oci-a100-kvm/deployment.json stop --no-restore-guests
TimeoutStartSec=1200
TimeoutStopSec=900
UMask=0077

[Install]
WantedBy=multi-user.target
'''


def policy_text(text):
    for key, value in (('ON_BOOT', 'ignore'), ('ON_SHUTDOWN', 'shutdown')):
        pattern = rf'^[ \t]*{key}=.*$'
        if re.search(pattern, text, re.M):
            text = re.sub(pattern, f'{key}={value}', text, flags=re.M)
        else:
            text += f'\n{key}={value}\n'
    return text


def ensure_dir(path):
    trusted_path(path, allow_missing=True, directory=True)
    path.mkdir(mode=0o755, parents=True, exist_ok=True)
    trusted_path(path, directory=True)


def install(config_path):
    config = root_config(config_path)
    search_path = '/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/sbin:/usr/local/bin'
    for binary in ('systemctl', 'virsh', 'virt-xml-validate', 'nvidia-smi', 'nvswitch-audit', 'fuser', 'modprobe', 'ip'):
        resolved = shutil.which(binary, path=search_path)
        if resolved is None:
            raise RuntimeError('Required host tool is missing: ' + binary)
    trusted_path(config['paths']['fm_client'])
    trusted_path(config['paths']['fm_config'])
    if run(['systemctl', 'is-active', 'a100-pcie-two-vm.service'], False).stdout.strip() in ('active', 'activating', 'deactivating'):
        raise RuntimeError('Lifecycle service is active; stop and review before updating installed code')
    if (Path(config['paths']['state_dir']) / 'manifest.json').exists():
        raise RuntimeError('Prepared deployment exists; this initial installer does not replace its code or configuration')
    source_root = Path(__file__).resolve().parents[1]
    sources = [p for p in source_root.rglob('*') if p.is_file() and p.suffix in ('.py', '.sh') and '__pycache__' not in p.parts]
    if not sources:
        raise RuntimeError('No helper sources found')
    for source in sources:
        if source.is_symlink() or any((source_root / p).is_symlink() for p in source.relative_to(source_root).parents):
            raise RuntimeError('Source symlinks are not supported')
    for guest in config['guests']:
        run(['virsh', '-c', 'qemu:///system', 'dominfo', guest['name']])
    # Validate every destination before the first write.
    for path in (UNIT, POLICY, CONFIG_DEST):
        trusted_path(path, allow_missing=True)
    trusted_path(INSTALL_ROOT, allow_missing=True, directory=True)
    trusted_path(LINK.parent, directory=True)
    if LINK.is_symlink():
        if LINK.readlink() != INSTALL_ROOT / 'host/gpu-vm-manager.py':
            raise RuntimeError('Existing manager symlink points elsewhere')
    elif LINK.exists():
        raise RuntimeError('Existing manager path is not the expected symlink')
    for source in sources:
        trusted_path(INSTALL_ROOT / source.relative_to(source_root), allow_missing=True)
    backup = Path('/var/backups') / ('oci-a100-kvm-install-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    trusted_path(backup, allow_missing=True, directory=True)
    backup.mkdir(mode=0o700)
    for path in (UNIT, POLICY, CONFIG_DEST):
        if path.exists():
            shutil.copy2(path, backup / path.name)
    for source in sources:
        target = INSTALL_ROOT / source.relative_to(source_root)
        ensure_dir(target.parent)
        shutil.copyfile(source, target)
        target.chmod(0o644 if target.parent.name == 'lib' else 0o755)
    ensure_dir(CONFIG_DEST.parent)
    if Path(config_path) != CONFIG_DEST:
        shutil.copyfile(config_path, CONFIG_DEST)
    CONFIG_DEST.chmod(0o600)
    UNIT.write_text(unit_text(config))
    UNIT.chmod(0o644)
    POLICY.write_text(policy_text(POLICY.read_text() if POLICY.exists() else ''))
    POLICY.chmod(0o644)
    if not LINK.exists():
        LINK.symlink_to(INSTALL_ROOT / 'host/gpu-vm-manager.py')
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'disable', 'a100-pcie-two-vm.service'])
    for guest in config['guests']:
        run(['virsh', '-c', 'qemu:///system', 'autostart', '--disable', guest['name']])
    print('Installed; lifecycle service remains disabled. Backup: ' + str(backup))
    print('Verify both guest initramfs files, then prepare, start and run acceptance checks before enabling boot startup.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(CONFIG_DEST))
    parser.add_argument('--execute', '--install', dest='execute', action='store_true', help='Apply installation after review')
    args = parser.parse_args()
    if not args.execute:
        print('Plan: install helpers under ' + str(INSTALL_ROOT))
        print('Plan: install disabled a100-pcie-two-vm.service; set libvirt-guests ON_BOOT=ignore and ON_SHUTDOWN=shutdown')
        print('Add --execute to apply. No changes made.')
        return
    install(args.config)


if __name__ == '__main__':
    main()
