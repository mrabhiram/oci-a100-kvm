#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
execute=0
target=/opt/oci-a100-kvm/nccl-tests
while [[ $# -gt 0 ]]; do
  case "$1" in
    --execute) execute=1; shift ;;
    --source-directory)
      [[ $# -ge 2 && -n $2 ]] || { printf 'Missing source-directory value.\n' >&2; exit 2; }
      target=$2; shift 2 ;;
    --help) printf '%s\n' 'Usage: install-test-tools.sh [--source-directory PATH] [--execute]' 'Plan by default. Root --execute installs the pinned NVIDIA CUDA repository keyring, CUDA 12.9 tools, NCCL 2.32.3 and builds nccl-tests v2.20.0.' 'Uses caller proxy environment. Refuses driver/kernel changes, removals or existing-package upgrades in the tooling transaction.' 'No GPU workload or reboot is performed; NVIDIA source retains its upstream license.'; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done
[[ $target == /* ]] || { printf 'Use an absolute source path.\n' >&2; exit 2; }
printf 'Plan: configure signed NVIDIA Ubuntu 24.04 repository; install CUDA nvcc 12.9.86/cudart 12.9.79 and NCCL 2.32.3; build pinned nccl-tests in %s.\n' "$target"
[[ $execute == 1 ]] || exit 0
[[ $EUID == 0 ]] || { printf 'Run --execute as root.\n' >&2; exit 1; }
. /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 && $(uname -m) == x86_64 ]] || { printf 'Only Ubuntu 24.04 x86_64 is supported.\n' >&2; exit 1; }
for command in curl sha256sum dpkg-deb python3 git make g++; do command -v "$command" >/dev/null || { printf 'Missing %s; complete guest prerequisites first.\n' "$command" >&2; exit 1; }; done
driver_before=$(dpkg-query -W -f='${Version}' nvidia-driver-580-server)
[[ $driver_before == 580.178.04-0ubuntu0.24.04.1 ]] || { printf 'Tested guest driver must already be installed.\n' >&2; exit 1; }
[[ ! -L $target ]] || { printf 'Refusing symlink source directory.\n' >&2; exit 1; }
export LC_ALL=C DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
export http_proxy="${http_proxy:-${HTTP_PROXY:-}}" https_proxy="${https_proxy:-${HTTPS_PROXY:-}}"
export no_proxy="${no_proxy:-${NO_PROXY:-}}"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
keyring=cuda-keyring_1.1-1_all.deb
# Digest recorded for the exact package in the accepted original build.
keyring_sha256=d2a6b11c096396d868758b86dab1823b25e14d70333f1dfa74da5ddaf6a06dba
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
  --connect-timeout 20 --max-time 180 --retry 2 \
  "https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/$keyring" -o "$work/$keyring"
(cd "$work" && printf '%s *%s\n' "$keyring_sha256" "$keyring" | sha256sum -c -)
[[ $(dpkg-deb --field "$work/$keyring" Package) == cuda-keyring && \
   $(dpkg-deb --field "$work/$keyring" Version) == 1.1-1 && \
   $(dpkg-deb --field "$work/$keyring" Architecture) == all ]] || { printf 'Unexpected keyring package identity.\n' >&2; exit 1; }
dpkg -i "$work/$keyring"
python3 - <<'PY'
from pathlib import Path
path = Path('/etc/apt/sources.list.d/cuda-ubuntu2404-x86_64.list')
lines = [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.lstrip().startswith('#')]
expected = 'deb [signed-by=/usr/share/keyrings/cuda-archive-keyring.gpg] https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/ /'
if lines != [expected] or not Path('/usr/share/keyrings/cuda-archive-keyring.gpg').is_file():
    raise SystemExit('NVIDIA repository must use the expected HTTPS origin and scoped signed-by keyring; inspect configuration.')
PY
apt-get -o Acquire::Retries=0 -o APT::Update::Error-Mode=any update
packages=(cuda-nvcc-12-9=12.9.86-1 cuda-cudart-dev-12-9=12.9.79-1 libnccl2=2.32.3-1+cuda12.9 libnccl-dev=2.32.3-1+cuda12.9)
apt-get --no-install-recommends --no-remove -s install "${packages[@]}" > "$work/apt-plan.txt"
python3 - "$work/apt-plan.txt" <<'PY'
from pathlib import Path
import sys
for line in Path(sys.argv[1]).read_text().splitlines():
    if line.startswith('Remv '):
        raise SystemExit('Package removal refused: ' + line)
    if line.startswith('Inst '):
        fields = line.split()
        if fields[1].startswith(('nvidia-', 'libnvidia-', 'linux-', 'cuda-drivers')) or fields[2].startswith('['):
            raise SystemExit('Driver/kernel change or existing-package upgrade refused: ' + line)
print('Tooling package simulation passed.')
PY
apt-get --no-install-recommends --no-remove -y install "${packages[@]}"
[[ $(dpkg-query -W -f='${Version}' nvidia-driver-580-server) == "$driver_before" ]] || { printf 'Driver changed unexpectedly.\n' >&2; exit 1; }
for spec in "${packages[@]}"; do
  [[ $(dpkg-query -W -f='${Version}' "${spec%%=*}") == "${spec#*=}" ]] || { printf 'Tooling version differs: %s\n' "${spec%%=*}" >&2; exit 1; }
done
dpkg --audit
url=https://github.com/NVIDIA/nccl-tests.git
commit=b4d5beebca8a76cf01335f724d154b9b9d394d96
if [[ ! -e $target ]]; then
  mkdir -p "$(dirname "$target")"
  git clone --branch v2.20.0 --depth 1 "$url" "$target"
fi
[[ $(git -C "$target" remote get-url origin) == "$url" ]] || { printf 'Unexpected nccl-tests origin.\n' >&2; exit 1; }
[[ $(git -C "$target" rev-parse HEAD) == "$commit" ]] || { printf 'nccl-tests commit differs from the tested pin.\n' >&2; exit 1; }
[[ -z $(git -C "$target" status --porcelain --untracked-files=no) ]] || { printf 'Tracked nccl-tests sources have local modifications.\n' >&2; exit 1; }
make -C "$target" -j8 CUDA_HOME=/usr/local/cuda-12.9 NCCL_HOME=/usr MPI=0 \
  NVCC_GENCODE='-gencode=arch=compute_80,code=sm_80'
[[ -x $target/build/all_reduce_perf ]]
printf 'Tools installed at %s/build/all_reduce_perf. Retain upstream NVIDIA license. No GPU tests have run.\n' "$target"
