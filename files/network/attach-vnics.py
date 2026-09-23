#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Attach two private OCI VNICs. Dry run unless --execute is given."""
import argparse
import ipaddress
import json
import re
import sys
import uuid


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('profile', 'region', 'instance-id', 'subnet-id'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--guest-names', nargs=2, required=True, metavar=('VM1', 'VM2'))
    parser.add_argument('--nic-index', type=int, default=0)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    require(len(set(args.guest_names)) == 2 and all(
        re.fullmatch(r'[a-z][a-z0-9-]{0,40}', name) for name in args.guest_names),
        'Use two different guest names containing lowercase letters, numbers and hyphens.')
    require(args.nic_index in (0, 1), 'NIC index must be 0 or 1.')
    try:
        import oci
    except ImportError:
        raise RuntimeError('The OCI Python SDK is required. Use your OCI CLI Python environment.')
    config = oci.config.from_file(profile_name=args.profile)
    config['region'] = args.region
    options = {'timeout': (10, 30), 'retry_strategy': oci.retry.NoneRetryStrategy()}
    compute = oci.core.ComputeClient(config, **options)
    network = oci.core.VirtualNetworkClient(config, **options)
    tenancy = oci.identity.IdentityClient(config, **options).get_tenancy(config['tenancy']).data
    instance = compute.get_instance(args.instance_id).data
    require(instance.shape == 'BM.GPU4.8' and instance.lifecycle_state == 'RUNNING',
            'The target must be a running BM.GPU4.8 instance.')
    subnet = network.get_subnet(args.subnet_id).data
    require(subnet.lifecycle_state == 'AVAILABLE', 'Subnet is not available.')
    require(subnet.availability_domain in (None, instance.availability_domain),
            'The subnet availability domain differs from the host.')
    prefix = ipaddress.ip_network(subnet.cidr_block).prefixlen
    attachments = oci.pagination.list_call_get_all_results(compute.list_vnic_attachments,
        compartment_id=instance.compartment_id, instance_id=args.instance_id).data
    active = [a for a in attachments if a.lifecycle_state != 'DETACHED']
    attached_vnics = {a.id: network.get_vnic(a.vnic_id).data for a in active
                      if a.lifecycle_state == 'ATTACHED'}
    primary = [v for v in attached_vnics.values() if v.is_primary]
    require(len(primary) == 1, 'Cannot identify exactly one primary VNIC.')
    primary_subnet = network.get_subnet(primary[0].subnet_id).data
    require(subnet.vcn_id == primary_subnet.vcn_id, 'Use a subnet in the host VCN.')

    def validate(a, v):
        require(a.lifecycle_state == 'ATTACHED' and v.lifecycle_state == 'AVAILABLE',
                'Attachment is not ready. Reconcile it in OCI before rerunning.')
        require(v.subnet_id == args.subnet_id and v.public_ip is None and not v.is_primary,
                'Existing VNIC differs from the requested private secondary VNIC.')
        require(v.skip_source_dest_check is False and a.nic_index == args.nic_index,
                'VNIC source checks or physical NIC index differ from the request.')
        require(a.vlan_tag and 1 <= a.vlan_tag <= 4094, 'VLAN attachment tag is missing.')

    planned = []
    # Check both names before creating either VNIC.
    for guest in args.guest_names:
        matches = [a for a in active if a.display_name == guest + '-vnic']
        require(len(matches) <= 1, 'Multiple attachments have the requested display name.')
        a = matches[0] if matches else None
        if a:
            require(a.lifecycle_state == 'ATTACHED',
                    'An attachment is in flight. Reconcile it in OCI before rerunning.')
            validate(a, attached_vnics[a.id])
        planned.append((guest, a))
    print(json.dumps({'profile': args.profile, 'region': args.region, 'tenancy': tenancy.name,
                      'shape': instance.shape, 'execute': args.execute,
                      'plan': [{'guest': name, 'action': 'reuse' if a else 'attach private VNIC'}
                               for name, a in planned]}, indent=2), flush=True)
    if not args.execute:
        return
    for guest, attachment in planned:
        if attachment is None:
            details = oci.core.models.AttachVnicDetails(
                instance_id=args.instance_id, display_name=guest + '-vnic', nic_index=args.nic_index,
                create_vnic_details=oci.core.models.CreateVnicDetails(
                    subnet_id=args.subnet_id, display_name=guest + '-vnic', assign_public_ip=False,
                    assign_private_dns_record=False, skip_source_dest_check=False))
            token = str(uuid.uuid5(uuid.NAMESPACE_URL, '/'.join(
                (args.instance_id, args.subnet_id, guest, str(args.nic_index)))))
            try:
                attachment = compute.attach_vnic(details, opc_retry_token=token).data
            except Exception as exc:
                raise RuntimeError('Attachment creation returned an error. It may have succeeded. '
                                   'No retry or cleanup was attempted; inspect OCI attachments '
                                   'before rerunning.') from exc
            attachment = oci.wait_until(compute, compute.get_vnic_attachment(attachment.id),
                'lifecycle_state', 'ATTACHED', max_interval_seconds=5, max_wait_seconds=180).data
        vnic = network.get_vnic(attachment.vnic_id).data
        validate(attachment, vnic)
        print(json.dumps({'guest': guest, 'vnic': {
            'id': vnic.id, 'ip': vnic.private_ip, 'prefix': prefix,
            'gateway': subnet.virtual_router_ip, 'mac': vnic.mac_address.lower(),
            'vlan_tag': attachment.vlan_tag, 'nic_index': attachment.nic_index}}, indent=2), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
