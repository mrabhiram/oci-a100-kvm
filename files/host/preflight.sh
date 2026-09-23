#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Read-only host checks. Nothing is installed, saved, or changed.
set -euo pipefail
export LC_ALL=C
if [[ ${1:-} == --help ]]; then
  printf '%s\n' 'Usage: preflight.sh [--help]' 'Read-only Ubuntu/KVM, CPU, memory and PCI checks; no files are created.'
  exit 0
fi
[[ $# == 0 ]] || { printf 'Unexpected argument. Use --help.\n' >&2; exit 2; }
missing=0
for command in lscpu free lspci; do
  command -v "$command" >/dev/null || { printf 'Missing prerequisite: %s\n' "$command" >&2; missing=1; }
done
[[ $missing == 0 ]] || exit 1
printf 'OS / kernel\n'
if [[ -r /etc/os-release ]]; then
  . /etc/os-release
  printf '%s\n' "${PRETTY_NAME:-unknown}"
fi
uname -r
printf '\nCPU topology / memory\n'
lscpu
free -h
printf '\nVirtualization prerequisites\n'
if [[ -c /dev/kvm ]]; then printf '/dev/kvm is present\n'; else printf '/dev/kvm is absent\n'; missing=1; fi
if grep -Eq '\b(svm|vmx)\b' /proc/cpuinfo; then printf 'CPU virtualization flag present\n'; else printf 'CPU virtualization flag absent\n'; missing=1; fi
printf '\nNVIDIA PCI devices / ownership\n'
lspci -Dnn -d 10de:
gpu_count=0
for device in /sys/bus/pci/devices/*; do
  [[ $(cat "$device/vendor") == 0x10de ]] || continue
  kind=$(cat "$device/device")
  [[ $kind != 0x20b0 ]] || gpu_count=$((gpu_count + 1))
  driver=unbound; group=absent
  [[ ! -L $device/driver ]] || driver=$(basename "$(readlink "$device/driver")")
  [[ ! -L $device/iommu_group ]] || group=$(basename "$(readlink "$device/iommu_group")")
  if [[ $kind == 0x20b0 && $group == absent ]]; then missing=1; fi
  printf '%s device=%s driver=%s iommu=%s numa=%s reset=%s\n' \
    "${device##*/}" "$kind" "$driver" "$group" "$(cat "$device/numa_node")" \
    "$(cat "$device/reset_method" 2>/dev/null || printf unavailable)"
done
printf 'A100 0x20b0 devices: %s (expected 8 for BM.GPU4.8)\n' "$gpu_count"
[[ $gpu_count == 8 ]] || missing=1
printf '\nStorage capacity\n'
df -h / /var/lib
printf '\nInstalled virtualization tools\n'
if command -v qemu-system-x86_64 >/dev/null; then qemu-system-x86_64 --version; fi
if command -v virsh >/dev/null; then virsh --version; fi
printf '\nThis inventory does not validate ACS/reset isolation, GPU health, guest workloads or capacity reservations.\n'
exit "$missing"
