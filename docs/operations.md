# Operations

Use these commands after completing the [deployment guide](deployment.md). The manager reads `/etc/oci-a100-kvm/deployment.json` by default. Review configured guest names before a restart.

## Inspect

On the host:

```sh
sudo /usr/local/sbin/a100-pcie-two-vm inspect
sudo virsh list --all
sudo systemctl status a100-pcie-two-vm.service --no-pager
```

Inside each guest:

```sh
nvidia-smi -L
grep '^NvLinkDisable:' /proc/driver/nvidia/params
systemctl --failed --no-pager
```

Expect four assigned GPUs and `NvLinkDisable: 1` in each guest. During guest operation, the host's GPUs are VFIO-owned, so host `nvidia-smi` is not the guest GPU inventory check. The oneshot lifecycle service's `active/exited` state records startup completion; it is not continuous GPU health monitoring.

## Start, stop and restart

Drain applications before changing VM state. For an installed deployment:

```sh
# Stop both GPU guests and restore NVIDIA ownership on the host.
sudo systemctl stop a100-pcie-two-vm.service

# Start both GPU guests through the checked lifecycle.
sudo systemctl start a100-pcie-two-vm.service

# Restart one guest; use its actual configured domain name.
sudo /usr/local/sbin/a100-pcie-two-vm restart <guest-name>
```

Service stop leaves the workload guests off and restores the host baseline, including mode-0 Fabric Manager. Validate host CUDA before using that baseline for compute. Domain autostart stays disabled because the lifecycle service controls startup.

## Interrupted transitions

A timeout or lost SSH session does not prove the remote operation stopped. Inspect its recorded phase, process ownership, libvirt jobs and guest boot identity before retrying. Preserve the manifest and protected backups; do not delete uncertainty markers to bypass checks or issue a second restart while the first remains active.

Configuration/initramfs snapshots do not back up VM disks or application data. Kernel, driver and firmware changes require renewed validation. See [troubleshooting](troubleshooting.md) and [validation](validation.md).

[Documentation index](README.md)
