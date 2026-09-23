# Troubleshooting

Problems found during the original build and the fixes included in this package.

| Symptom | Finding and correction |
|---|---|
| `kvm_amd: Operation not supported`; `SVM disabled (by BIOS)` | CPU virtualization was disabled. Enable virtualization instructions in the applicable platform settings and reboot; changing GPU drivers does not address this. |
| Ubuntu cannot locate `virtinst`, `cloud-image-utils` or related packages | Inspect enabled Ubuntu repositories, package indexes and download access. The completed installation used the required Noble repository components. |
| Driver installation loses its SSH connection | A launcher failure alone does not prove installation failed. Inspect the original job, installed packages and driver/GPU checks before repeating changes. |
| A guest shutdown exceeds a 30-second libvirt observation timeout | The original QEMU shutdown completed cleanly after about 40 seconds. Observation deadlines were increased to 120 seconds; explicit same-target recovery first verified stopped state, no outstanding owner/job, and a healthy peer. No forced PCI reset was used. |
| SSH disconnects during a guest restart | The original remote controller continued and completed. Inspect it instead of issuing another restart. The transport root cause was not established. |
| Service stop waits while restoring host GPU services | The corrected lifecycle unit removed FM/persistence ordering dependencies that conflicted with its synchronous stop/restoration path. |
| NCCL reports `SHM/direct` | Expected in this accepted NVLink-disabled configuration. Direct GPU peer access was unavailable; successful collectives do not demonstrate NVLink or direct PCIe P2P. |
| Bare-metal reboot takes many minutes | The validated reboot recovered automatically. A stale serial-console firmware message alone did not establish a stalled boot. Observe the existing operation and verify a new boot identity before taking another power action. |

The earlier full-fabric/service-VM experiments are excluded from the accepted startup path. Do not apply their cleanup or GPU/switch assignment scripts to this design.

[Operations](operations.md) · [Documentation index](README.md)
