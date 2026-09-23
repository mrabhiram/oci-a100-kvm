#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
execute=0
destination=/var/lib/libvirt/images/base/ubuntu-24.04-20260911
keyring=/usr/share/keyrings/ubuntu-cloudimage-keyring.gpg
while [[ $# -gt 0 ]]; do
  case "$1" in
    --execute) execute=1; shift ;;
    --output-directory|--keyring)
      [[ $# -ge 2 && -n $2 ]] || { printf 'Missing value for %s\n' "$1" >&2; exit 2; }
      if [[ $1 == --output-directory ]]; then destination=$2; else keyring=$2; fi; shift 2 ;;
    --help) printf '%s\n' 'Usage: prepare-image.sh [--output-directory PATH] [--keyring PATH] [--execute]' 'Plan by default; --execute requires root. Uses the preinstalled trusted Ubuntu cloud-image keyring.' 'Pins release 20260911, image checksum and Canonical signing fingerprint; never imports a downloaded trust key.' 'curl honors the caller proxy environment.'; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done
[[ $destination == /* && $keyring == /* ]] || { printf 'Use absolute output/keyring paths.\n' >&2; exit 2; }
url=https://cloud-images.ubuntu.com/releases/noble/release-20260911
image=ubuntu-24.04-server-cloudimg-amd64.img
expected=612b2c0cc1bc413a6cb8c38fd611794caf0f2b436c50013d8b3794db12ad7354
fingerprint=D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81
printf 'Plan: verify Canonical signature and pinned SHA256 for release 20260911; save verified image to %s.\n' "$destination"
[[ $execute == 1 ]] || exit 0
[[ $EUID == 0 ]] || { printf 'Run --execute as root.\n' >&2; exit 1; }
for command in curl gpgv sha256sum qemu-img python3; do command -v "$command" >/dev/null || { printf 'Missing %s\n' "$command" >&2; exit 1; }; done
[[ -f $keyring ]] || { printf 'Trusted Ubuntu cloud-image keyring missing; install it through trusted OS packages.\n' >&2; exit 1; }
[[ ! -L $destination && ! -L $destination/$image ]] || { printf 'Refusing a symlink output.\n' >&2; exit 1; }
install -d -m 0755 "$destination"
work=$(mktemp -d "$destination/.verify.XXXXXXXX")
trap 'rm -rf -- "$work"' EXIT
fetch=(curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --connect-timeout 20 --max-time 3600 --retry 2)
"${fetch[@]}" "$url/SHA256SUMS" -o "$work/SHA256SUMS"
"${fetch[@]}" "$url/SHA256SUMS.gpg" -o "$work/SHA256SUMS.gpg"
gpgv --status-fd 1 --keyring "$keyring" "$work/SHA256SUMS.gpg" "$work/SHA256SUMS" > "$work/signature-verification.txt"
grep -q "^\[GNUPG:\] VALIDSIG $fingerprint " "$work/signature-verification.txt"
python3 - "$work/SHA256SUMS" "$expected" "$image" <<'PY'
from pathlib import Path
import sys
matches = [line.split(maxsplit=1) for line in Path(sys.argv[1]).read_text().splitlines() if line.strip()]
matches = [digest for digest, name in matches if name.lstrip('*') == sys.argv[3]]
if matches != [sys.argv[2]]:
    raise SystemExit('Signed manifest does not contain the exact pinned image checksum.')
PY
if [[ -e $destination/$image ]]; then
  (cd "$destination" && printf '%s *%s\n' "$expected" "$image" | sha256sum -c -)
else
  "${fetch[@]}" "$url/$image" -o "$work/$image"
  (cd "$work" && printf '%s *%s\n' "$expected" "$image" | sha256sum -c -)
  chmod 0444 "$work/$image"
  mv -- "$work/$image" "$destination/$image"
fi
for artifact in SHA256SUMS SHA256SUMS.gpg signature-verification.txt; do
  [[ ! -L $destination/$artifact ]] || { printf 'Refusing symlink verification record.\n' >&2; exit 1; }
  install -m 0444 "$work/$artifact" "$destination/$artifact"
done
qemu-img info --output=json "$destination/$image"
printf 'Verified image: %s/%s\n' "$destination" "$image"
