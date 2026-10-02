#!/bin/bash
set -euo pipefail

inp="$HPC_TASK_INPUT"
case "$inp" in
  *.inp) ;;
  *) echo "not an ORCA .inp file: $inp" >&2; exit 2 ;;
esac
dir=$(dirname "$inp")
base=$(basename "$inp" .inp)
out="$dir/$base.out"

# Parallel ORCA has to be started by its full path.
if ! ORCA=$(command -v orca); then
  echo "orca is not on PATH; set this template's \"modules\" (search_modules finds them)" >&2
  exit 3
fi

# The core count in the input must match what SLURM gave the job.
ntasks="${SLURM_NTASKS:-$HPC_PARAM_NPROCS}"
want=$(grep -ioE 'nprocs[[:space:]]+[0-9]+' "$inp" | head -n 1 | grep -oE '[0-9]+$' || true)
if [ -n "$want" ] && [ "$want" != "$ntasks" ]; then
  echo "$inp asks for $want cores but the job has $ntasks; resubmit with nprocs=$want" >&2
  exit 4
fi
if [ -z "$want" ] && [ "$ntasks" != "1" ]; then
  echo "warning: $inp has no %pal block, so ORCA will use 1 of the $ntasks allocated cores" >&2
fi

# Run in node-local scratch when there is one; ORCA writes many temporary files.
work="${TMPDIR:-$dir}/orca-${SLURM_JOB_ID:-local}-${SLURM_ARRAY_TASK_ID:-0}"
mkdir -p "$work"
trap 'rm -rf "$work"' EXIT
cp "$inp" "$work/"
for extra in "$dir/$base.gbw" "$dir/$base.xyz"; do   # restart files, if a previous attempt left them
  if [ -f "$extra" ]; then cp "$extra" "$work/"; fi
done
cd "$work"

status=0
"$ORCA" "$base.inp" > "$out" 2>&1 || status=$?

for f in "$base".*; do
  case "$f" in
    "$base.inp"|*.tmp|*.tmp.*|*.densities*) ;;
    *) cp -p "$f" "$dir/" ;;
  esac
done

if [ "$status" -ne 0 ] || ! grep -q "ORCA TERMINATED NORMALLY" "$out"; then
  echo "ORCA did not finish normally (exit $status); see $out" >&2
  exit 1
fi
