#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
execute=0
backup=/var/lib/oci-a100-kvm/guest-preparation
while [[ $# -gt 0 ]]; do
  case "$1" in
    --execute) execute=1; shift ;;
    --backup-directory)
      [[ $# -ge 2 && -n $2 ]] || { printf 'Missing backup-directory value.\n' >&2; exit 2; }
      backup=$2; shift 2 ;;
    --help) printf '%s\n' 'Usage: configure-gpu.sh [--backup-directory PATH] [--execute]' 'Prepare a CPU-only Ubuntu guest: save its current initramfs, set NVreg_NvLinkDisable=1, rebuild and verify embedded configuration.' 'Default is plan only; --execute requires root and driver 580.178.04. Existing backups/configuration are not overwritten.' 'No reboot or GPU assignment is performed. Verify effective NvLinkDisable after the later guest boot.'; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done
[[ $backup == /* ]] || { printf 'Use an absolute backup path.\n' >&2; exit 2; }
printf 'Plan: preserve current guest initramfs in %s; write /etc/modprobe.d/99-a100-pcie-only.conf, rebuild and verify NVLink disable.\n' "$backup"
[[ $execute == 1 ]] || exit 0
[[ $EUID == 0 ]] || { printf 'Run --execute as root.\n' >&2; exit 1; }
for command in python3 modinfo update-initramfs lsinitramfs unmkinitramfs; do command -v "$command" >/dev/null || { printf 'Missing %s\n' "$command" >&2; exit 1; }; done
python3 - "$backup" <<'PY'
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

def require(ok, message):
    if not ok:
        raise SystemExit(message)

def output(*args):
    return subprocess.check_output(args, text=True, timeout=120).strip()

kernel = os.uname().release
backup = Path(sys.argv[1])
config = Path('/etc/modprobe.d/99-a100-pcie-only.conf')
initrd = Path('/boot') / ('initrd.img-' + kernel)
require(output('modinfo', '-F', 'version', 'nvidia') == '580.178.04', 'Expected driver 580.178.04.')
require(re.search(r'^NVreg_NvLinkDisable:', output('modinfo', '-F', 'parm', 'nvidia'), re.M), 'Driver parameter unavailable.')
for device in Path('/sys/bus/pci/devices').iterdir():
    require(not ((device / 'vendor').read_text().strip() == '0x10de' and
                 (device / 'device').read_text().strip() == '0x20b0'), 'A100 already assigned; prepare a CPU-only guest.')
require(not backup.exists() and not backup.is_symlink(), 'Backup path already exists; inspect before repeating preparation.')
require(not config.exists() and not config.is_symlink(), 'Existing configuration will not be overwritten.')
for path in Path('/etc/modprobe.d').glob('*.conf'):
    require(not any('NVreg_NvLinkDisable' in line and not line.lstrip().startswith('#')
                    for line in path.read_text().splitlines()), 'Conflicting NVLink setting in ' + str(path))
require(initrd.is_file() and not initrd.is_symlink(), 'Current initramfs must be a regular file.')
backup.mkdir(parents=True, mode=0o700)
backup.chmod(0o700)
saved = backup / initrd.name
shutil.copy2(initrd, saved)
digest = hashlib.sha256(initrd.read_bytes()).hexdigest()
require(hashlib.sha256(saved.read_bytes()).hexdigest() == digest, 'Original initramfs backup checksum mismatch.')
state = {'createdUtc': datetime.now(timezone.utc).isoformat(), 'kernel': kernel,
         'originalInitrdSha256': digest, 'configWasAbsent': True, 'verified': False}
record = backup / 'state.json'
record.write_text(json.dumps(state, indent=2) + '\n')
record.chmod(0o600)
contents = '# Whole-GPU PCIe-only guest; NVLink disabled.\noptions nvidia NVreg_NvLinkDisable=1\n'
try:
    config.write_text(contents)
    config.chmod(0o644)
    subprocess.run(['update-initramfs', '-u', '-k', kernel], check=True, timeout=600)
    listing = output('lsinitramfs', str(initrd)).splitlines()
    require('etc/modprobe.d/' + config.name in listing, 'NVLink configuration missing from rebuilt initramfs.')
    with tempfile.TemporaryDirectory(prefix='oci-a100-initramfs-') as temp:
        subprocess.run(['unmkinitramfs', str(initrd), temp], check=True, timeout=180)
        copies = list(Path(temp).rglob(config.name))
        require(bool(copies) and all(path.read_text() == contents for path in copies),
                'Rebuilt initramfs does not contain the expected configuration bytes.')
    state.update(verified=True, preparedInitrdSha256=hashlib.sha256(initrd.read_bytes()).hexdigest())
    record.write_text(json.dumps(state, indent=2) + '\n')
except BaseException:
    config.unlink(missing_ok=True)
    shutil.copy2(saved, initrd)
    print('Preparation failed; restored the original initramfs and removed the new configuration.', file=sys.stderr)
    raise
print('Initramfs backup and embedded NVLink-disable setting verified. No reboot or assignment performed.')
print('After later guest boot, verify NvLinkDisable: 1 in /proc/driver/nvidia/params.')
PY
