# Recorded validation

The original installation passed final acceptance on **23 September 2026 at 21:35:08 UTC**. The tables below describe that installation. The extracted repository scripts have offline tests and have not been deployed as a complete package to a fresh host. Hardware tests must be repeated for each new installation.

## Package checks

The extracted scripts pass Python and shell syntax checks, 40 offline tests, and local documentation-link checks. Tests exercise configuration conflicts, ownership guards, restart uncertainty, generated domain XML, CPU/NUMA placement and VNIC metadata checks. CI runs the same offline checks. No new hardware run is claimed for this package.

## Tested software

| Component | Recorded version |
|---|---|
| Host OS / kernel | Ubuntu 24.04.5 LTS / `6.17.0-1020-oracle` |
| Guest OS / kernel | Ubuntu 24.04.5 LTS / `6.8.0-139-generic` |
| Canonical guest cloud-image release | `20260911`; signature and checksum verified |
| QEMU | `8.2.2`; Ubuntu package `1:8.2.2+ds-0ubuntu1.18` |
| libvirt package | `10.0.0-2ubuntu8.16` |
| Host and guest NVIDIA driver | `580.178.04` |
| Host Fabric Manager / SDK package | `580.178.04-0ubuntu0.24.04.1` |
| CUDA validation tools | nvcc `12.9.86`; cudart `12.9.79` |
| NCCL / nccl-tests | `2.32.3+cuda12.9` / `v2.20.0` |

The driver's reported CUDA 13.0/API 13000 capability is distinct from the installed CUDA validation-tool versions.

## Completed gates

| Check | Original lab result |
|---|---|
| Allocation and ownership | Disjoint four-GPU and 56-vCPU assignments; expected RAM, private VNICs, stock reset methods and accessible host-owned switches |
| Guest readiness | Expected GPU identities, driver, effective NVLink disable, cloud-init completion and OS resources verified |
| Concurrent CUDA | Kernels and host/device copies passed on all four GPUs in each guest |
| Concurrent NCCL | Separate four-GPU all-reduces passed in each guest; wrong-value and out-of-bounds counts were zero |
| Individual manager restarts | Both guests passed fresh-boot identity and CUDA checks |
| Ordinary guest OS reboots | Both returned through private SSH with changed boot identities and passed CUDA checks |
| Normal service stop/start | Full eight-GPU host restoration and host CUDA passed; both GPU guests then returned and passed CUDA |
| One controlled host reboot | Automatic two-guest startup, retained private networking and final concurrent CUDA/NCCL passed |
| Cleanup and final health checks | No active GPU test processes or failed guest systemd units; no matching current-boot GPU kernel errors |

The final concurrent run completed at 21:34:56 UTC. Clock-margin-adjusted command-process overlap was **9.75494 seconds for CUDA** and **42.66095 seconds for NCCL**, exceeding the respective 1-second and 30-second gates. These measurements establish overlapping validated command lifetimes, not simultaneous kernel occupancy or performance capacity.

Both guests used NCCL `SHM/direct` through their own CPU memory. Direct GPU peer access was unavailable; no NVLink/NVSwitch performance or cross-VM collective was established. Final checks found zero uncorrected ECC and zero volatile corrected ECC; one GPU retained an unchanged historical aggregate corrected ECC count of 2. This was not an all-counter-zero result or an extended hardware burn-in.

## Evidence limits

The recorded sequence includes an initial clock-estimation failure before workload execution, a corrected observation timeout during one restart, and an SSH disconnect while another remote restart continued. Independent inspection established safe state before continuing; the SSH disconnect cause was not diagnosed. The final accepted checks passed after those corrections.

Exactly one controlled host reboot formed the final acceptance gate. The observed interval from clean service stop to automatic two-VM startup was approximately 17 minutes 23 seconds; it is not a boot-time SLA.

Customer-model performance, production benchmarks, long-duration reliability, RDMA/GPUDirect, live migration, failover, tenant-isolation certification and VM-disk/application recovery were not tested. The results do not establish vendor certification or a production support commitment. A refactored package, changed software or another host requires new dated validation.

See [architecture](architecture.md), [networking](networking.md) and the [documentation index](README.md).
