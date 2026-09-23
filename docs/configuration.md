# Configuration

Copy [deployment.example.json](../manifests/deployment.example.json) to `/etc/oci-a100-kvm/deployment.json` on the host. Keep it root-owned and not writable by other users. It contains local infrastructure details and must not be committed.

The loader rejects unknown fields, placeholders, duplicate GPUs, overlapping guest CPU assignments, conflicting addresses and invalid paths. Runtime checks also compare the configuration with the actual host. Passing schema validation alone is not hardware validation.

| Field | Value to supply |
|---|---|
| `host` | Physical interface, OCI NIC index, current primary CIDR, MTU and reserved host RAM |
| `paths.base_dir` | Root-owned directory for build records |
| `paths.state_dir` | Separate root-owned lifecycle state directory |
| `paths.image`, `image_sha256` | Verified cloud image path and signed checksum |
| `paths.image_root` | Parent directory for independent guest disks |
| `paths.fm_client`, `fm_config` | Installed FM client and host FM configuration |
| `management` | Isolated libvirt network name, bridge and unused host CIDR |
| `ssh_public_key` | Root-owned file containing one guest SSH public key |
| `switches` | The six actual NVSwitch PCI addresses |
| `guests[].name` | Unique libvirt domain name |
| `memory_mib`, `disk_gib` | Guest RAM and virtual disk capacity |
| `cpu_pins` | Host logical CPUs in guest-vCPU order, adjacent real SMT sibling pairs |
| `emulator_cpus` | CPUs for that QEMU emulator; never another guest's allocation |
| `numa_nodes` | Host NUMA nodes for that guest; disjoint across guests |
| `partition_id` | Optional expected FM partition ID; null matches the exact GPU UUID set at startup |
| `gpus` | Four explicit `{bdf, uuid}` entries per guest |
| `guests[].management` | Guest IP in the isolated management subnet and a unique MAC |
| `vnic` | Actual OCI VNIC ID, IP, prefix, gateway, MAC, VLAN tag and NIC index |

Run discovery while all eight GPUs are host-owned. Review topology before choosing the two GPU groups. The CPU and address values in the example are examples, not defaults safe for another host.

The tested allocation was 56 vCPUs, 768 GiB RAM and a 200-GiB disk per guest. The package permits reviewed resource values within its checks, but those checks do not make every allocation a tested configuration.

Preparation binds a configuration hash to the saved state. Do not edit the configuration under an active deployment or modify the manifest to bypass a mismatch. Stop and review changes through the documented lifecycle first.

[Deployment](deployment.md) · [Documentation index](README.md)
