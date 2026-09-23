#!/usr/bin/env python3
"""Read host topology for an operator-reviewed configuration; never assign devices."""
import argparse
import csv
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

PCI = Path('/sys/bus/pci/devices')


def normalize_bdf(raw):
    match = re.fullmatch(r'(?:0000)?([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7])', raw.strip())
    if not match:
        raise ValueError('Invalid PCI address: ' + raw)
    return match.group(1).lower()


def command(args):
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=60).stdout


def read(path):
    return path.read_text().strip() if path.exists() else None


def pci_record(bdf, pci=PCI):
    path = pci / bdf
    group = path / 'iommu_group'
    return {
        'bdf': bdf, 'vendor': read(path / 'vendor'), 'device': read(path / 'device'),
        'numa_node': read(path / 'numa_node'),
        'driver': (path / 'driver').resolve().name if (path / 'driver').exists() else None,
        'iommu_group': group.resolve().name if group.exists() else None,
        'iommu_members': sorted(p.name for p in (group / 'devices').glob('*')),
        'reset_methods': read(path / 'reset_method'),
    }


def inventory(include_vnics=False):
    rows = csv.reader(io.StringIO(command([
        'nvidia-smi', '--query-gpu=pci.bus_id,uuid,name,memory.total',
        '--format=csv,noheader,nounits'])))
    gpus = []
    for row in rows:
        if len(row) != 4:
            raise ValueError('Unexpected nvidia-smi CSV layout')
        bdf, uid, name, memory = (x.strip() for x in row)
        item = pci_record(normalize_bdf(bdf))
        item.update(uuid=uid, name=name, memory_mib=int(memory))
        gpus.append(item)
    switches = [pci_record(p.name) for p in sorted(PCI.glob('*'))
                if read(p / 'vendor') == '0x10de' and read(p / 'device') == '0x1af1']
    if len(gpus) != 8 or len(switches) != 6 or any('A100' not in x['name'] for x in gpus):
        raise RuntimeError('Expected eight host-owned A100s and six NVSwitches; inspect the host baseline first')
    result = {
        'gpus': gpus, 'switches': switches,
        'cpus': json.loads(command(['lscpu', '-J', '-e=CPU,NODE,SOCKET,CORE,ONLINE'])),
        'memory': command(['free', '-b']),
        'addresses': json.loads(command(['ip', '-j', 'address'])),
        'routes': json.loads(command(['ip', '-j', 'route', 'show', 'table', 'all'])),
        'numa_memory': {p.parent.name: p.read_text() for p in
                        sorted(Path('/sys/devices/system/node').glob('node*/meminfo'))},
    }
    if include_vnics:
        request = urllib.request.Request('http://169.254.169.254/opc/v2/vnics/',
                                         headers={'Authorization': 'Bearer Oracle'})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=10) as response:
            vnics = json.load(response)
        fields = ('vnicId', 'nicIndex', 'vlanTag', 'macAddr', 'privateIp',
                  'subnetCidrBlock', 'virtualRouterIp')
        result['vnics'] = [{k: v.get(k) for k in fields} for v in vnics]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--include-vnics', action='store_true',
                        help='Read projected OCI VNIC metadata from IMDS')
    args = parser.parse_args()
    try:
        print(json.dumps(inventory(args.include_vnics), indent=2))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'Discovery failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
