# Dependencies

Project code is under the [MIT license](../LICENSE). Drivers, operating-system packages and upstream tools retain their own licenses. They are downloaded from their publishers; this repository does not bundle driver binaries, images or third-party source trees.

| Dependency | Source |
|---|---|
| Ubuntu cloud images and signing material | [Canonical cloud images](https://cloud-images.ubuntu.com/releases/noble/) and the Ubuntu cloud-image keyring package |
| NVIDIA driver and Fabric Manager | Ubuntu package repositories; [Fabric Manager documentation](https://docs.nvidia.com/datacenter/tesla/fabric-manager-user-guide/index.html) |
| FM partition client | [NVIDIA Fabric-Manager-Client](https://github.com/NVIDIA/Fabric-Manager-Client), pinned by the build script |
| CUDA and NCCL | [NVIDIA Ubuntu 24.04 repository](https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/) |
| Collective tests | [NVIDIA nccl-tests](https://github.com/NVIDIA/nccl-tests), pinned by the test-tool installer |
| Optional OCI API helper | [OCI Python SDK](https://github.com/oracle/oci-python-sdk), pinned in `requirements-oci.txt` |

See [tested versions](validation.md). If an exact package or image is no longer available, stop and select a newly validated combination. Do not silently substitute a new driver or skip signature checks.

[Documentation index](README.md)
