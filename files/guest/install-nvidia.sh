#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
execute=0
version=580.178.04-0ubuntu0.24.04.1
for arg in "$@"; do
  case "$arg" in
    --execute) execute=1 ;;
    --help) printf '%s\n' 'Usage: install-nvidia.sh [--execute]' 'Guest only: install exact NVIDIA driver 580.178.04 and current-kernel headers on Ubuntu 24.04.' 'Prepare the CPU-only guest before assigning A100s. Default is plan only; --execute requires root.' 'Uses caller proxy environment; does not reboot or install Fabric Manager.'; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$arg" >&2; exit 2 ;;
  esac
done
printf 'Plan: install current-kernel headers, guest utilities and NVIDIA driver %s; verify DKMS/module version.\n' "$version"
[[ $execute == 1 ]] || exit 0
[[ $EUID == 0 ]] || { printf 'Run --execute as root.\n' >&2; exit 1; }
. /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 && $(uname -m) == x86_64 ]] || { printf 'Only Ubuntu 24.04 x86_64 is supported.\n' >&2; exit 1; }
for device in /sys/bus/pci/devices/*; do
  if [[ $(cat "$device/vendor") == 0x10de && $(cat "$device/device") == 0x20b0 ]]; then
    printf 'An A100 is already assigned. Prepare the CPU-only guest first.\n' >&2; exit 1
  fi
done
export http_proxy="${http_proxy:-${HTTP_PROXY:-}}" https_proxy="${https_proxy:-${HTTPS_PROXY:-}}"
export no_proxy="${no_proxy:-${NO_PROXY:-}}" DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
apt-get -o Acquire::Retries=0 -o APT::Update::Error-Mode=any update
apt-get install -y --no-install-recommends --no-remove \
  "linux-headers-$(uname -r)" "nvidia-driver-580-server=$version" \
  pciutils numactl curl ca-certificates jq python3 git build-essential pkg-config initramfs-tools
[[ $(dpkg-query -W -f='${Version}' nvidia-driver-580-server) == "$version" ]] || exit 1
[[ $(modinfo -F version nvidia) == 580.178.04 ]] || { printf 'DKMS module version check failed.\n' >&2; exit 1; }
[[ -d /usr/src/linux-headers-$(uname -r) ]] || { printf 'Current-kernel headers missing.\n' >&2; exit 1; }
dpkg --audit
dkms status
printf 'Guest driver installed. Configure and verify NVLink disable before GPU assignment; no reboot was requested.\n'
