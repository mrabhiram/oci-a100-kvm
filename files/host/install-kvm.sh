#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
execute=0
for arg in "$@"; do
  case "$arg" in
    --execute) execute=1 ;;
    --help) printf '%s\n' 'Usage: install-kvm.sh [--execute]' 'Default: print the plan. With --execute, run as root on Ubuntu 24.04.' 'Uses caller HTTP(S)_PROXY/http(s)_proxy settings; never reboots.'; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$arg" >&2; exit 2 ;;
  esac
done
printf '%s\n' 'Plan: update Ubuntu package indexes; install KVM/QEMU/libvirt, OVMF, image verification and build prerequisites; enable libvirtd; validate KVM.'
[[ $execute == 1 ]] || exit 0
[[ $EUID == 0 ]] || { printf 'Run --execute as root.\n' >&2; exit 1; }
. /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 && $(uname -m) == x86_64 ]] || { printf 'Only Ubuntu 24.04 x86_64 is supported.\n' >&2; exit 1; }
export http_proxy="${http_proxy:-${HTTP_PROXY:-}}" https_proxy="${https_proxy:-${HTTPS_PROXY:-}}"
export no_proxy="${no_proxy:-${NO_PROXY:-}}"
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
apt-get -o Acquire::Retries=0 -o APT::Update::Error-Mode=any update
apt-get install -y --no-install-recommends --no-remove \
  qemu-system-x86 qemu-utils libvirt-daemon-system libvirt-clients virtinst ovmf \
  cloud-image-utils ubuntu-cloudimage-keyring osinfo-db cpu-checker pciutils \
  numactl iproute2 curl ca-certificates jq python3 git gnupg gpgv \
  build-essential pkg-config libjsoncpp-dev libxml2-utils psmisc
systemctl enable --now libvirtd
kvm-ok
virt-host-validate qemu
printf 'KVM prerequisites installed. No guest or GPU assignment was created.\n'
