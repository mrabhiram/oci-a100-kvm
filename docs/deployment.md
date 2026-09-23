# Deployment

Run this procedure on an idle BM.GPU4.8 host with Ubuntu 24.04. The scripts use whole GPUs with NVLink disabled. The original installation passed GPU and reboot tests; this extracted package needs its own hardware acceptance on each new installation.

## 1. Prepare the host

Enable virtualization instructions, IOMMU and ACS in the applicable OCI platform settings, then apply the required reboot. Confirm CPU virtualization and an IOMMU are available. Keep SSH access through an existing bastion or private network.

Obtain the repository on the host:

```sh
git clone https://github.com/mrabhiram/oci-a100-kvm.git
cd oci-a100-kvm
bash files/host/preflight.sh
```

Ensure the Ubuntu `main`, `restricted`, `universe` and `multiverse` repository components and package-download access are available. Install KVM, the matched NVIDIA driver/Fabric Manager packages and the FM client:

```sh
sudo bash files/host/install-kvm.sh --execute
sudo bash files/host/install-nvidia.sh --execute
sudo bash files/host/build-fm-client.sh --execute
sudo bash files/host/prepare-image.sh --execute
```

These scripts show their plan without `--execute`. They use the tested package and image versions, fail if those versions are unavailable, and do not reboot the host. The image script checks the Canonical signature and checksum using the installed Ubuntu cloud-image keyring. Do not replace failed verification with an unsigned download.

Verify eight host-owned GPUs and working KVM before continuing:

```sh
sudo kvm-ok
nvidia-smi -L
nvidia-smi
sudo virt-host-validate qemu
```

## 2. Attach private VNICs

Use the OCI Console to attach two secondary VNICs without public IPs, or use the optional helper from a workstation with an OCI profile and the SDK in `requirements-oci.txt`:

```sh
python3 files/network/attach-vnics.py \
  --profile PROFILE --region REGION \
  --instance-id INSTANCE_OCID --subnet-id SUBNET_OCID \
  --guest-names a100-vm01 a100-vm02
```

Replace the uppercase values. The first call reads OCI and shows a plan; append `--execute` to attach the VNICs. The helper keeps the existing security rules and routes. It refuses conflicting attachments. After a timeout, inspect the original attachment before retrying.

See [networking](networking.md) for SSH rules, Linux VLAN tags and package-download access.

## 3. Review the host configuration

On the host, collect the GPU, CPU, NUMA and VNIC details:

```sh
sudo python3 files/host/discover.py --include-vnics > inventory.private.json
sudo install -d -m 0755 /etc/oci-a100-kvm
sudo install -m 0600 manifests/deployment.example.json \
  /etc/oci-a100-kvm/deployment.json
sudoedit /etc/oci-a100-kvm/deployment.json
```

The example is intentionally incomplete. Replace all placeholders and review every sample address, CPU list and path. Use [configuration](configuration.md) for the fields. Place only the SSH **public** key at the configured `ssh_public_key` path, owned by root and not writable by other users.

Choose two disjoint sets of four GPUs from this host. Review their NUMA placement and exclusive IOMMU/reset domains. Use actual SMT sibling pairs for the vCPU pins; retain host CPU and memory capacity. Do not copy another host's GPU UUIDs, PCI addresses or VLAN tags.

Validate the completed configuration:

```sh
sudo python3 files/lib/config.py /etc/oci-a100-kvm/deployment.json
```

## 4. Create CPU-only guests

Preview and then create the guests:

```sh
sudo python3 files/host/create-guests.py \
  --config /etc/oci-a100-kvm/deployment.json
sudo python3 files/host/create-guests.py \
  --config /etc/oci-a100-kvm/deployment.json --execute
```

The preflight checks image integrity, CPU/NUMA placement, memory, network metadata and existing storage before creating guests. Existing guests or files require inspection; this is not a general rebuild command. The new guests initially have no GPU devices.

Verify SSH, `cloud-init status --wait`, CPU, memory and disk size in both guests. Provide package-download access before the next step.

## 5. Prepare each guest

Copy this repository to each guest through your SSH path. In each guest, from the repository root:

```sh
sudo bash files/guest/install-nvidia.sh --execute
sudo bash files/guest/configure-gpu.sh --execute
sudo bash files/guest/install-test-tools.sh --execute
```

The GPU preparation saves the current initramfs, sets `NVreg_NvLinkDisable=1`, rebuilds it and verifies the embedded setting. It refuses to run after an A100 has been attached. Preserve the backups. A partially completed setup needs inspection before another attempt.

## 6. Install the lifecycle service and start the GPU guests

On the host:

```sh
sudo python3 files/host/install-services.py \
  --config /etc/oci-a100-kvm/deployment.json --execute
sudo /usr/local/sbin/a100-pcie-two-vm prepare --guests-nvlink-disabled
sudo systemctl start a100-pcie-two-vm.service
sudo /usr/local/sbin/a100-pcie-two-vm inspect
```

The `prepare` flag confirms that both guest initramfs files were prepared and verified. It is not a substitute for those checks. Preparation saves the CPU-only baseline and must not be repeated over existing state.

The manager checks GPU identities, switch ownership and inactive fabric partitions before VFIO assignment. All six switches remain on the host. It resolves a partition by the exact reviewed GPU UUID set when `partition_id` is null; it never chooses a different GPU group for you.

## 7. Validate and enable boot startup

Inside each guest:

```sh
nvidia-smi -L
grep '^NvLinkDisable:' /proc/driver/nvidia/params
python3 test/cuda-smoke.py --execute
bash test/nccl.sh --execute
```

Expect four correct GPU identities and `NvLinkDisable: 1`. Run compute checks in both guests concurrently and inspect their results. Follow [test/README.md](../test/README.md) and [operations](operations.md) for restarts, full host GPU restoration and service recovery.

Only after those checks pass, enable the service on the host:

```sh
sudo systemctl enable a100-pcie-two-vm.service
```

Schedule one controlled host reboot, then verify changed boot identities, automatic guest startup, private SSH, exact GPU assignments and fresh concurrent CUDA/NCCL results. Do not treat an active service or a running VM alone as GPU health proof.

[Documentation index](README.md)
