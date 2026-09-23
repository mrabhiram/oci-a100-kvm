"""Strict deployment configuration. Loading performs no host changes."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat


class ConfigError(ValueError):
    pass


def require(ok, message):
    if not ok:
        raise ConfigError(message)


def keys(value, expected, label):
    require(isinstance(value, dict), f'{label} must be an object')
    require(set(value) == set(expected.split()), f'{label}: missing or unknown fields: {set(value) ^ set(expected.split())}')


def integer(value, low, high, label):
    require(type(value) is int and low <= value <= high, f'{label}: integer {low}..{high} required')


def string(value, pattern, label):
    require(isinstance(value, str) and re.fullmatch(pattern, value) is not None, f'{label}: invalid value')
    require(not re.search(r'CHANGE_ME|REPLACE|<|>|\bTBD\b', value, re.I), f'{label}: replace placeholders')


def absolute(value, label):
    string(value, r'/[A-Za-z0-9_./-]+', label)
    p = Path(value)
    require(p.is_absolute() and '..' not in p.parts and value == str(p) and value != '/', f'{label}: normalized absolute path required')


def ipv4(value, label):
    require(isinstance(value, str), f'{label}: string required')
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise ConfigError(f'{label}: invalid IP address') from exc
    require(address.version == 4 and not address.is_multicast and not address.is_unspecified and not address.is_loopback, f'{label}: usable IPv4 address required')
    return address


def interface(value, label):
    require(isinstance(value, str) and '/' in value, f'{label}: CIDR string required')
    try:
        address = ipaddress.ip_interface(value)
    except ValueError as exc:
        raise ConfigError(f'{label}: invalid IPv4 CIDR') from exc
    ipv4(str(address.ip), label)
    require(address.version == 4 and 1 <= address.network.prefixlen <= 30 and address.ip not in (address.network.network_address, address.network.broadcast_address), f'{label}: usable IPv4 host CIDR required')
    return address


def ints(value, label):
    require(isinstance(value, list) and value, f'{label}: nonempty list required')
    for item in value:
        integer(item, 0, 65535, label)
    require(len(value) == len(set(value)), f'{label}: duplicate values')


def validate_config(c):
    keys(c, 'schema_version host paths management ssh_public_key switches guests', 'config')
    require(type(c['schema_version']) is int and c['schema_version'] == 1, 'schema_version must be 1')
    h = c['host']
    keys(h, 'interface nic_index primary_cidr mtu reserve_memory_gib', 'host')
    string(h['interface'], r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,14}', 'host.interface')
    integer(h['nic_index'], 0, 15, 'host.nic_index')
    primary = interface(h['primary_cidr'], 'host.primary_cidr')
    integer(h['mtu'], 1280, 9000, 'host.mtu')
    integer(h['reserve_memory_gib'], 32, 2048, 'host.reserve_memory_gib')
    keys(c['paths'], 'base_dir state_dir image image_sha256 fm_client fm_config image_root', 'paths')
    for key, value in c['paths'].items():
        if key == 'image_sha256':
            string(value, r'[a-f0-9]{64}', key)
            require(len(set(value)) > 1, 'image_sha256: replace placeholder digest')
        else:
            absolute(value, 'paths.' + key)
    require(c['paths']['state_dir'] != c['paths']['base_dir'], 'state_dir must be separate from base_dir')
    absolute(c['ssh_public_key'], 'ssh_public_key')
    m = c['management']
    keys(m, 'name bridge host_cidr', 'management')
    string(m['name'], r'[a-z][a-z0-9-]{0,47}', 'management.name')
    string(m['bridge'], r'[a-z][a-z0-9-]{0,14}', 'management.bridge')
    management = interface(m['host_cidr'], 'management.host_cidr')
    require(not management.network.overlaps(primary.network), 'management subnet overlaps host subnet')
    require(isinstance(c['switches'], list) and len(c['switches']) == 6, 'Exactly six NVSwitch BDFs required')
    for bdf in c['switches']:
        string(bdf, r'[0-9a-f]{4}:[0-9a-f]{2}:[0-1][0-9a-f]\.[0-7]', 'switch BDF')
    require(len(set(c['switches'])) == 6, 'Duplicate NVSwitch BDF')
    require(isinstance(c['guests'], list) and len(c['guests']) == 2, 'Exactly two guests required')
    allocated_cpus, bdfs, uuids, names, macs, addresses, tags, vnics, partitions = set(), set(c['switches']), set(), set(), set(), {str(primary.ip), str(management.ip)}, set(), set(), set()
    for g in c['guests']:
        keys(g, 'name memory_mib disk_gib cpu_pins emulator_cpus numa_nodes partition_id gpus management vnic', 'guest')
        string(g['name'], r'[a-z][a-z0-9-]{0,47}', 'guest.name')
        require(g['name'] not in names, 'Duplicate guest name')
        names.add(g['name'])
        integer(g['memory_mib'], 4096, 2 * 1024 * 1024, 'memory_mib')
        integer(g['disk_gib'], 40, 65536, 'disk_gib')
        for key in ('cpu_pins', 'emulator_cpus', 'numa_nodes'):
            ints(g[key], key)
        require(len(g['cpu_pins']) % 2 == 0, 'cpu_pins must contain whole SMT pairs')
        require(not allocated_cpus.intersection(g['cpu_pins']), 'Guest CPU assignments overlap')
        allocated_cpus.update(g['cpu_pins'])
        if g['partition_id'] is not None:
            integer(g['partition_id'], 0, 65535, 'partition_id')
            require(g['partition_id'] not in partitions, 'Duplicate FM partition_id')
            partitions.add(g['partition_id'])
        require(isinstance(g['gpus'], list) and len(g['gpus']) == 4, 'Exactly four GPUs per guest required')
        for gpu in g['gpus']:
            keys(gpu, 'bdf uuid', 'GPU')
            string(gpu['bdf'], r'[0-9a-f]{4}:[0-9a-f]{2}:[0-1][0-9a-f]\.[0-7]', 'GPU BDF')
            string(gpu['uuid'], r'GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', 'GPU UUID')
            require(gpu['bdf'] not in bdfs and gpu['uuid'] not in uuids, 'Duplicate GPU BDF or UUID')
            require(len(set(gpu['uuid'][4:].replace('-', ''))) > 1, 'GPU UUID is a placeholder')
            bdfs.add(gpu['bdf'])
            uuids.add(gpu['uuid'])
        keys(g['management'], 'ip mac', 'guest.management')
        keys(g['vnic'], 'id ip prefix gateway mac vlan_tag nic_index', 'guest.vnic')
        v = g['vnic']
        string(v['id'], r'ocid1\.vnic\.[a-z0-9.]+', 'vnic.id')
        require(v['id'] not in vnics, 'Duplicate VNIC ID')
        vnics.add(v['id'])
        integer(v['prefix'], 1, 30, 'vnic.prefix')
        integer(v['vlan_tag'], 1, 4094, 'vnic.vlan_tag')
        require(v['vlan_tag'] not in tags, 'Duplicate VNIC VLAN tag')
        tags.add(v['vlan_tag'])
        integer(v['nic_index'], 0, 15, 'vnic.nic_index')
        require(v['nic_index'] == h['nic_index'], 'VNIC must use the configured physical NIC')
        vnic_address = interface(f'{v["ip"]}/{v["prefix"]}', 'vnic address')
        gateway = ipv4(v['gateway'], 'vnic.gateway')
        require(gateway in vnic_address.network and gateway != vnic_address.ip, 'VNIC gateway must be another address in its subnet')
        require(not management.network.overlaps(vnic_address.network), 'management subnet overlaps VNIC subnet')
        for kind in ('management', 'vnic'):
            n = g[kind]
            ip = ipv4(n['ip'], kind + '.ip')
            require(str(ip) not in addresses, 'Duplicate host or guest IP')
            addresses.add(str(ip))
            if kind == 'management':
                require(ip in management.network and ip not in (management.network.network_address, management.network.broadcast_address), 'Guest management IP is outside management subnet')
            string(n['mac'], r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', kind + '.mac')
            require(int(n['mac'][:2], 16) & 1 == 0 and n['mac'] != '00:00:00:00:00:00', 'Unicast MAC required')
            require(n['mac'] not in macs, 'Duplicate MAC')
            macs.add(n['mac'])
    for guest, other in ((c['guests'][0], c['guests'][1]), (c['guests'][1], c['guests'][0])):
        require(not set(guest['emulator_cpus']).intersection(other['cpu_pins'] + other['emulator_cpus']), 'Emulator CPUs overlap the other guest')
    require(not set(c['guests'][0]['numa_nodes']).intersection(c['guests'][1]['numa_nodes']), 'Guest NUMA allocations overlap')
    return c


def load_config(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f'Duplicate JSON key: {key}')
            result[key] = value
        return result
    return validate_config(json.loads(Path(path).read_text(), object_pairs_hook=unique))


def config_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def trusted_path(path, *, allow_missing=False, directory=False):
    """Reject symlinks and non-root-writable ancestors before root operations."""
    path = Path(path)
    absolute(str(path), 'trusted path')
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            require(allow_missing, f'Missing trusted path: {part}')
            continue
        require(not stat.S_ISLNK(info.st_mode), f'Symlink not allowed: {part}')
        require(info.st_uid == 0 and not info.st_mode & 0o022, f'Path must be root-owned and not group/world writable: {part}')
        if part != path:
            require(stat.S_ISDIR(info.st_mode), f'Ancestor is not a directory: {part}')
    if path.exists():
        require(path.is_dir() if directory else path.is_file(), f'Wrong path type: {path}')
    return path


def root_config(path):
    require(os.geteuid() == 0, 'Run with sudo')
    trusted_path(path)
    return load_config(path)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Validate deployment JSON without changing the host')
    parser.add_argument('config')
    args = parser.parse_args()
    config = load_config(args.config)
    print('Configuration valid; SHA256 ' + config_hash(config))
