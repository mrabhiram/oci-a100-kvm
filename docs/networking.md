# Networking

Each guest gets a private IP through an OCI secondary VNIC. The setup scripts verify attachment metadata before creating Linux links.

## Workload connectivity

Each guest uses a private-IP-only secondary OCI VNIC attached to the bare-metal instance. The corresponding Linux VLAN interface feeds a macvtap passthrough connection, presented to the guest as a virtio network interface. Guest MAC, IP, prefix and gateway configuration come from the OCI attachment metadata.

The Linux VLAN tags are assigned attachment details. **No separate OCI VLAN resource was created.** They should be discovered and validated for the current attachments, not copied from another host. The lab's VNIC-link helper checked attachment identity and prevented the guest IP from also being configured on the host.

The host's primary management VNIC, OCI security rules and routes were retained. Each guest also has a second virtio interface on an isolated, host-only libvirt management bridge with no forwarding.

## Private addresses and access

A guest having no public IP does not establish that its OCI subnet is a private subnet. The lab reused an existing subnet with an Internet Gateway default route and an existing publicly addressed jump host. The guests were reached through the existing jump path; no new public guest address was assigned.

The host-only management network is separate from the OCI workload network. Its address range must be selected to avoid existing routes. The setup validates the physical parent NIC, attachment tags, MAC addresses, private addresses, gateway and management-network range before configuring links.

## Access rules

For SSH through a jump VM in the same VCN, allow stateful TCP ingress to guest destination port 22 from the jump VM's **private** IP `/32`. If the jump VM's egress is restricted, allow TCP destination port 22 to each guest IP `/32`. Source ports are unrestricted. Apply the rules to the guest VNIC's NSG or subnet security list. Also verify the guest firewall and SSH service.

Use your existing SSH identity and jump path, replacing the uppercase values:

```sh
ssh -J JUMP_USER@JUMP_PUBLIC_IP ubuntu@GUEST_PRIVATE_IP
```

The attachment helper needs OCI access to read the tenancy, target instance, subnet and VNICs, and to attach secondary VNICs in the target compartment. Keep permissions scoped to those resources. This project does not change IAM policies, security lists, NSGs or routes.

## Package downloads and recovery

Temporary repository tunnels supported package installation and were closed afterward. No permanent guest internet-egress path was added. An Internet Gateway route alone should not be presented as proof that these private-IP-only guests can download packages; the deployment procedure must document the actual approved download path.

During the single accepted host-reboot test, the VNIC-link units and host-only management network recovered, both guests retained their OCI private addresses, and private SSH returned. This is the original lab's observed recovery result, not a general network-availability guarantee.

See [architecture](architecture.md), [recorded validation](validation.md) and the [documentation index](README.md).
