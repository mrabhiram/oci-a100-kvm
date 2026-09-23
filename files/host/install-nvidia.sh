#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
execute=0
version=580.178.04-0ubuntu0.24.04.1
for arg in "$@"; do
  case "$arg" in
    --execute) execute=1 ;;
    --help) printf '%s\n' 'Usage: install-nvidia.sh [--execute]' 'Host only: Ubuntu 24.04, eight A100s, no running guests or GPU workloads.' 'Installs exact tested driver/FM/SDK 580.178.04 and current-kernel headers.' 'Default is plan only. Uses caller proxy environment. Never reboots.'; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$arg" >&2; exit 2 ;;
  esac
done
printf 'Plan: install current-kernel headers and matched NVIDIA driver, Fabric Manager and SDK %s; verify loaded driver and start Fabric Manager.\n' "$version"
[[ $execute == 1 ]] || exit 0
[[ $EUID == 0 ]] || { printf 'Run --execute as root.\n' >&2; exit 1; }
. /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 && $(uname -m) == x86_64 ]] || { printf 'Only Ubuntu 24.04 x86_64 is supported.\n' >&2; exit 1; }
[[ -c /dev/kvm ]] || { printf '/dev/kvm is missing; complete host preflight first.\n' >&2; exit 1; }
if command -v virsh >/dev/null; then
  running=$(virsh -c qemu:///system list --name)
  [[ -z $running ]] || { printf 'Stop running guests before host driver installation.\n' >&2; exit 1; }
fi
count=0
for device in /sys/bus/pci/devices/*; do
  [[ $(cat "$device/vendor") == 0x10de && $(cat "$device/device") == 0x20b0 ]] || continue
  count=$((count + 1))
  if [[ -L $device/driver && $(basename "$(readlink "$device/driver")") == vfio-pci ]]; then
    printf 'An A100 is VFIO-owned; restore host ownership first.\n' >&2; exit 1
  fi
done
[[ $count == 8 ]] || { printf 'Expected eight A100 0x20b0 devices.\n' >&2; exit 1; }
if command -v nvidia-smi >/dev/null; then
  apps=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader)
  [[ -z $apps ]] || { printf 'GPU workloads are present.\n' >&2; exit 1; }
fi
export http_proxy="${http_proxy:-${HTTP_PROXY:-}}" https_proxy="${https_proxy:-${HTTPS_PROXY:-}}"
export no_proxy="${no_proxy:-${NO_PROXY:-}}" DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
apt-get -o Acquire::Retries=0 -o APT::Update::Error-Mode=any update
apt-get install -y --no-install-recommends --no-remove "linux-headers-$(uname -r)" \
  "nvidia-driver-580-server=$version" "nvidia-fabricmanager-580=$version" "nvidia-fabricmanager-dev-580=$version"
[[ $(modinfo -F version nvidia) == 580.178.04 ]] || { printf 'Installed module version does not match.\n' >&2; exit 1; }
modprobe nvidia
loaded=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | sort -u)
[[ $loaded == 580.178.04 ]] || { printf 'Loaded driver differs; review required maintenance. No reboot was requested.\n' >&2; exit 1; }
systemctl enable --now nvidia-fabricmanager.service
systemctl is-active --quiet nvidia-fabricmanager.service
for package in nvidia-driver-580-server nvidia-fabricmanager-580 nvidia-fabricmanager-dev-580; do
  [[ $(dpkg-query -W -f='${Version}' "$package") == "$version" ]] || exit 1
done
dpkg --audit
nvidia-smi --query-gpu=index,name,driver_version,memory.total --format=csv
printf 'Matched host packages and Fabric Manager verified. Separate fabric/GPU acceptance is still required.\n'
