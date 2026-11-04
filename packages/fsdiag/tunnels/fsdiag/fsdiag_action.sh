#!/usr/bin/env bash
# What actions.json runs, as you, from this directory (the REST server runs it for an agent; no shell, typed arguments):
#   fsdiag_action.sh probe REPEATS                  JSON findings and report for the node this runs on
#   fsdiag_action.sh survey FOLDER SECONDS          the same for a folder's contents, and the quota commands
#   fsdiag_action.sh requests HOURS SLOW_MS         what the REST server's slow requests were waiting for
#   fsdiag_action.sh submit COMMENT NODE REPEATS    submit a probe job (prints its ID); its output is the text report
#   fsdiag_action.sh report JOB_ID                  print that job's report (the agent can't open the file itself)
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
settings="${HPCTUNNELS_SETTINGS_DIR:-$HOME/.local/tunnels/settings}/fsdiag.sh"
[ -f "$settings" ] && source "$settings"      # the app's saved settings (python, partition, account...)
source "$here/user.sh"
data="${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}"
cli="$here/fsdiag_cli.py"
command="$1"
shift
case "$command" in
  probe)
    exec "$FSDIAG_PYTHON" "$cli" --json probe --repeats "$1" ;;
  survey)
    # latency is the probe's job: this counts, and reads the quota
    exec "$FSDIAG_PYTHON" "$cli" --json probe --no-latency --survey "$1" --survey-seconds "$2" ;;
  requests)
    exec "$FSDIAG_PYTHON" "$cli" --json requests --hours "$1" --slow-ms "$2" ;;
  submit)
    comment="$1" node="$2" repeats="$3"
    mkdir -p "$data/sessions/fsdiag" || exit 1
    args=(--parsable --job-name=hpclib-fsdiag --nodes=1 --ntasks=1 --cpus-per-task=1 "--mem=$FSDIAG_MEM" "--time=$FSDIAG_TIME_LIMIT"
          "--output=$data/sessions/fsdiag/probe-%j.txt" "$comment")
    [ -n "$FSDIAG_PARTITION" ] && args+=("--partition=$FSDIAG_PARTITION")
    [ -n "$FSDIAG_ACCOUNT" ] && args+=("--account=$FSDIAG_ACCOUNT")
    [ "$node" != any ] && args+=("--nodelist=$node")
    # the job's output is the report; the compute python may differ from the login node's
    exec sbatch "${args[@]}" --wrap "exec '$FSDIAG_COMPUTE_PYTHON' '$cli' probe --repeats '$repeats'" ;;
  report)
    file="$data/sessions/fsdiag/probe-$1.txt"
    [[ "$1" =~ ^[0-9]+$ ]] || { echo "fsdiag_action.sh: job IDs are numbers" >&2; exit 2; }
    [ -s "$file" ] || { echo "no report for job $1 yet: it hasn't run, or hasn't finished (job_status says which)" >&2; exit 1; }
    exec cat "$file" ;;
  *)
    echo "fsdiag_action.sh: unknown command '$command'" >&2
    exit 2 ;;
esac
