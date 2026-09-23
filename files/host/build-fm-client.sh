#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
execute=0
source_dir=/opt/oci-a100-kvm/Fabric-Manager-Client
prefix=/usr/local/libexec/oci-a100-kvm
commit=8ec6fb8e2aa7457aae00d1a309f26863bd6d9bcb
while [[ $# -gt 0 ]]; do
  case "$1" in
    --execute) execute=1; shift ;;
    --source-directory|--prefix)
      [[ $# -ge 2 && -n $2 ]] || { printf 'Missing value for %s\n' "$1" >&2; exit 2; }
      if [[ $1 == --source-directory ]]; then source_dir=$2; else prefix=$2; fi; shift 2 ;;
    --help) printf '%s\n' 'Usage: build-fm-client.sh [--source-directory PATH] [--prefix PATH] [--execute]' 'Build the public NVIDIA Fabric-Manager-Client at the tested commit against FM SDK 580.178.04.' 'Plan by default; root required for --execute. Requires git, g++, pkg-config, libjsoncpp-dev and matched FM SDK.' 'NVIDIA source retains its upstream license; no vendor binary is shipped in this repository.'; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done
[[ $source_dir == /* && $prefix == /* ]] || { printf 'Use absolute source/prefix paths.\n' >&2; exit 2; }
printf 'Plan: fetch NVIDIA/Fabric-Manager-Client commit %s, build locally, install %s/fmpm.\n' "$commit" "$prefix"
[[ $execute == 1 ]] || exit 0
[[ $EUID == 0 ]] || { printf 'Run --execute as root.\n' >&2; exit 1; }
for command in git g++ pkg-config; do command -v "$command" >/dev/null || { printf 'Missing %s\n' "$command" >&2; exit 1; }; done
[[ $(dpkg-query -W -f='${Version}' nvidia-fabricmanager-dev-580) == 580.178.04-0ubuntu0.24.04.1 ]] || { printf 'Matched tested Fabric Manager SDK is required.\n' >&2; exit 1; }
[[ ! -L $source_dir && ! -L $prefix && ! -L $prefix/fmpm ]] || { printf 'Refusing symlink installation paths.\n' >&2; exit 1; }
export http_proxy="${http_proxy:-${HTTP_PROXY:-}}" https_proxy="${https_proxy:-${HTTPS_PROXY:-}}"
url=https://github.com/NVIDIA/Fabric-Manager-Client.git
if [[ ! -e $source_dir ]]; then
  mkdir -p "$(dirname "$source_dir")"
  git clone --no-checkout "$url" "$source_dir"
  git -C "$source_dir" checkout --detach "$commit"
fi
[[ $(git -C "$source_dir" remote get-url origin) == "$url" ]] || { printf 'Unexpected source remote.\n' >&2; exit 1; }
[[ $(git -C "$source_dir" rev-parse HEAD) == "$commit" ]] || { printf 'Source commit differs from the tested pin.\n' >&2; exit 1; }
[[ -z $(git -C "$source_dir" status --porcelain --untracked-files=no) ]] || { printf 'Tracked NVIDIA sources have local modifications.\n' >&2; exit 1; }
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
# Keep the NVIDIA repository and its license intact; build output is separate.
read -r -a json_flags <<< "$(pkg-config --cflags jsoncpp)"
g++ -O2 -I/usr/include "${json_flags[@]}" "$source_dir/fmpm.cpp" -lnvfm -ljsoncpp -o "$work/fmpm"
"$work/fmpm" -v
install -d -m 0755 "$prefix"
install -m 0755 "$work/fmpm" "$prefix/fmpm"
printf 'Built from NVIDIA source at %s. Retain and review its upstream license in %s.\n' "$commit" "$source_dir"
