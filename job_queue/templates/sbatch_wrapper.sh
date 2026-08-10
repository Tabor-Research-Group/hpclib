#!/bin/bash
# sbatch_wrapper.sh
#
# Drop this logic into the top of any real sbatch script. It decides,
# via the installed `job-queue` command, whether this job needs a fresh
# submission, a restart, is already done, or has permanently failed -
# then sources the matching branch script.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

JOB_NAME="my_job"
METADATA="metadata/${JOB_NAME}.json"

status_log=$(mktemp)
status=$(job-queue status --metadata "${METADATA}" --job-name "${JOB_NAME}" 2>"${status_log}")

case "${status}" in
    submit)
        source "${SCRIPT_DIR}/submit.sh"
        ;;
    restart)
        source "${SCRIPT_DIR}/restart.sh"
        ;;
    running)
        echo "Job ${JOB_NAME} is already active; nothing to do."
        cat "${status_log}"
        ;;
    complete)
        echo "Job ${JOB_NAME} has already finished."
        ;;
    error)
        echo "Job ${JOB_NAME} failed permanently:"
        cat "${status_log}"
        rm -f "${status_log}"
        exit 1
        ;;
    *)
        echo "Unrecognized status '${status}' from job-queue status" >&2
        cat "${status_log}" >&2
        rm -f "${status_log}"
        exit 1
        ;;
esac

rm -f "${status_log}"
