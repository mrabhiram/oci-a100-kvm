# Validation tools

The extracted package has not been run on a new host. The original proof of concept passed bounded GPU and lifecycle checks; see [recorded validation](../docs/validation.md) for its evidence and limits.

These tools default to a printed plan. Add `--execute` only on the intended idle guest. They do not assign GPUs, reboot, or stop existing workloads. The workload tests need no root privileges. All commands below are relative to the repository root.

## Offline checks

Run from the repository root with Python 3.12 or later:

```sh
python3 -m compileall -q files test
python3 -m unittest discover -s test/offline -v
python3 test/check-repository.py
find files test -name '*.sh' -exec bash -n {} \;
```

CI runs these checks without OCI credentials or GPU access. Tests cover configuration conflicts, device ownership, interrupted operations, domain XML, CPU/NUMA allocation and network metadata. They do not replace hardware acceptance.

## Install pinned tooling in each CPU-only guest

```sh
bash files/guest/install-nvidia.sh
bash files/guest/install-test-tools.sh
sudo bash files/guest/install-nvidia.sh --execute
sudo bash files/guest/install-test-tools.sh --execute
```

The tooling installer downloads NVIDIA's CUDA keyring package `1.1-1` over HTTPS and verifies its SHA256 against the original build before installation. APT uses the NVIDIA repository's scoped signing key. It installs CUDA nvcc `12.9.86`, cudart `12.9.79`, NCCL `2.32.3+cuda12.9`, and builds NVIDIA nccl-tests `v2.20.0` at its pinned commit. It refuses tooling transactions that remove packages, upgrade existing packages, or change NVIDIA drivers or kernels. No GPU workload runs during installation.

The default nccl-tests source directory is `/opt/oci-a100-kvm/nccl-tests`; override it with `--source-directory`. The downloaded NVIDIA sources and their upstream licenses remain separate from this repository's MIT-licensed scripts. Package downloads honor the caller's proxy environment. If sudo strips that environment, preserve only the needed proxy variables using your approved workstation policy; no proxy address is embedded in the scripts.

## Check compute after assignment

```sh
python3 test/cuda-smoke.py --execute --expected-gpus 4
bash test/nccl.sh --execute --gpus 4
```

CUDA checks a small kernel and host/device copies on every visible GPU, and reports the peer-access capability matrix. It does not perform peer transfers. The CUDA test has a 180-second default process deadline; NCCL has a 180-second outer deadline and a 10-second termination grace period. A timeout is a failed check, not permission to reset devices or restart guests.

NCCL runs a single 16-MiB four-GPU all-reduce test and checks version, wrong-value counts and out-of-bounds results. Its log is printed. In the original build, `SHM/direct` meant CPU-memory staging within each guest; direct GPU P2P was unavailable. These commands do not synchronize two guests or prove concurrent execution, bandwidth, production model performance, or long-duration reliability.

Use `--binary` for a different nccl-tests installation path. Run fresh identity, driver, effective `NvLinkDisable`, workload, ownership and log checks after an approved lifecycle transition. The setup tools do not schedule host or guest reboots.

## Hardware acceptance

After assignment, record GPU UUIDs, effective NVLink-disable settings, driver versions and current-boot kernel errors. Run the CUDA and NCCL commands in both guests at the same time, preserving start/end times and results. An SSH connection closing is not a workload result.

Then check each guest restart, each guest OS reboot, normal pair stop/start, and host GPU restoration. After restoring the host, run the CUDA check there with `--expected-gpus 8`. Enable boot startup only after these checks pass, then perform one approved host reboot and repeat guest identity and compute checks. Use [operations](../docs/operations.md) for lifecycle commands.

Check that test processes have ended. Preserve any failure and its correction. Cross-VM collectives and production performance need separate tests.
