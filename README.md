# Running GPU Virtual Machines on OCI Bare Metal

Create two Ubuntu 24.04 KVM virtual machines on an existing OCI `BM.GPU4.8` instance, assigning four whole NVIDIA A100 GPUs to each VM through PCI passthrough.

## Validated Configuration

| Resource | Per VM |
|---|---|
| Operating system | Ubuntu 24.04 LTS |
| GPUs | 4 × A100 40 GB |
| CPU | 56 vCPUs |
| Memory | 768 GiB |
| Disk | 200 GiB |
| Network | Private IP through a secondary OCI VNIC |

The host also runs Ubuntu 24.04. This configuration uses the NVIDIA Data Center Driver and whole GPUs; it does not use vGPU Manager or MIG.

> NVLink is disabled. Validated multi-GPU communication uses CPU-memory staging; direct GPU peer access is unavailable. This is a tested proof of concept, not an Oracle or NVIDIA certified solution.

The original installation passed GPU and reboot tests. The extracted scripts have offline tests; a fresh installation still needs hardware validation. See [test scope](docs/validation.md).

## Prerequisites

- An existing `BM.GPU4.8` instance with virtualization instructions, IOMMU and ACS enabled.
- SSH and sudo access to the host, sufficient disk space, and package-download access for the host and guests.
- An existing VCN/subnet and permission to attach two secondary VNICs without public IPs.

## Deployment

Follow the [deployment guide](docs/deployment.md) to:

1. Validate the host and install KVM, NVIDIA drivers and Fabric Manager.
2. Configure private networking and create both Ubuntu guests.
3. Configure the NVLink-disabled GPU assignments and lifecycle service.
4. Verify CUDA/NCCL operation and recovery after guest and host reboots.

No separate OCI VLAN resource is required. Linux VLAN interfaces use the tags assigned to the secondary VNIC attachments. See [networking](docs/networking.md).

## Verification

Run inside each VM:

```sh
nvidia-smi -L
```

Expect four A100 GPUs. Follow the [validation guide](docs/validation.md) for compute and restart checks.

## Documentation

See the [documentation index](docs/README.md) for architecture, access, operations, troubleshooting and tested software versions.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

See [SECURITY.md](SECURITY.md).
