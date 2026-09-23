#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
set -euo pipefail
execute=0
binary=/opt/oci-a100-kvm/nccl-tests/build/all_reduce_perf
gpus=4
while [[ $# -gt 0 ]]; do
  case "$1" in
    --execute) execute=1; shift ;;
    --binary|--gpus)
      [[ $# -ge 2 && -n $2 ]] || { printf 'Missing value for %s\n' "$1" >&2; exit 2; }
      if [[ $1 == --binary ]]; then binary=$2; else gpus=$2; fi; shift 2 ;;
    --help) printf '%s\n' 'Usage: nccl.sh [--binary PATH] [--gpus 4] [--execute]' 'Plan by default. --execute runs a bounded in-guest all-reduce correctness test; no root or reboot needed.' 'Requires pinned nccl-tests v2.20.0/NCCL 2.32.3 installed by files/guest/install-test-tools.sh.' 'Logs are printed; SHM/direct is CPU-memory staging, not direct GPU P2P. No cross-VM test is performed.'; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done
[[ $gpus =~ ^[1-8]$ ]] || { printf 'GPU count must be 1 through 8.\n' >&2; exit 2; }
printf 'Plan: %s -b 16M -e 16M -f 2 -g %s -n 1000 -w 5 -c 1 -T 30; outer timeout 180 seconds.\n' "$binary" "$gpus"
[[ $execute == 1 ]] || exit 0
[[ -x $binary ]] || { printf 'NCCL binary missing; install pinned test tools first.\n' >&2; exit 1; }
for command in nvidia-smi timeout python3; do command -v "$command" >/dev/null || { printf 'Missing %s\n' "$command" >&2; exit 1; }; done
[[ -z $(nvidia-smi --query-compute-apps=pid --format=csv,noheader) ]] || { printf 'GPU compute processes already exist; use an idle validation guest.\n' >&2; exit 1; }
count=$(nvidia-smi --query-gpu=index --format=csv,noheader | wc -l)
[[ $count -eq $gpus ]] || { printf 'Visible NVIDIA device count differs from --gpus.\n' >&2; exit 1; }
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
export NCCL_DEBUG=INFO
unset NCCL_DEBUG_FILE
timeout --signal=TERM --kill-after=10s 180s "$binary" \
  -b 16M -e 16M -f 2 -g "$gpus" -n 1000 -w 5 -c 1 -T 30 2>&1 | tee "$work/nccl.log"
python3 - "$work/nccl.log" <<'PY'
from pathlib import Path
import re
import sys
text = Path(sys.argv[1]).read_text()
if not re.search(r'nccl-tests version 2\.20\.0 .*nccl-headers=23203 nccl-library=23203', text):
    raise SystemExit('Unexpected nccl-tests/NCCL version.')
if not re.search(r'# Out of bounds values\s*:\s*0\s+OK', text):
    raise SystemExit('Missing successful zero out-of-bounds summary.')
rows = [line.split() for line in text.splitlines() if re.match(r'^\s*16777216\s', line)]
if not rows or any(len(row) < 13 or row[8] != '0' or row[12] != '0' for row in rows):
    raise SystemExit('Missing or nonzero NCCL wrong-value results.')
print('NCCL correctness PASS. Review logged transport; this does not establish bandwidth or cross-VM performance.')
PY
