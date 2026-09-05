#!/bin/bash
# Clears everything the stress-test suite leaves behind, so it can be
# re-run from scratch:
#   - per-job metadata JSON files (metadata/*.json, relative to cwd)
#   - the shared queue.db (~/.local/share/job-queue by default, or
#     $JOB_QUEUE_HOME if you've set that)
#   - t4's scratch progress/checkpoint files
#
# Run this from the SAME directory you submit the tX_*.sh scripts
# from, since METADATA paths are relative to the submission cwd.

set -u

rm -rf metadata/
rm -rf t8_file_hooks/metadata/

JOB_QUEUE_HOME="${JOB_QUEUE_HOME:-$HOME/.local/share/job-queue}"
rm -f "$JOB_QUEUE_HOME/queue.db" "$JOB_QUEUE_HOME/queue.db-wal" "$JOB_QUEUE_HOME/queue.db-shm"

rm -rf "${TMPDIR:-/tmp}/jq_t4_work"

echo "Reset complete. Metadata, queue.db, and t4 scratch state cleared."
