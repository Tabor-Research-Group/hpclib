#! /bin/bash
# Runs on the compute node, in the ssh session that carries the tunnel's
# port forward. Shows the session log while the job is in the queue, then
# returns, so that ssh session (and the forward the login node holds for
# it) ends with the job instead of outliving it. Hanging up here cancels
# the job, as before, unless the tunnel only attached to it or keeps it.

job_active() {  # in the queue? transient squeue failures count as yes
  local out
  if out=$(squeue -h -j "$SESSION_ID" -o %i 2>&1); then
    [ -n "$out" ]
  else
    ! printf '%s' "$out" | grep -qi "invalid job id"
  fi
}

tail -f -n +1 "$SESSION_FILE" &
tail_pid=$!
finish() {
  kill "$tail_pid" 2>/dev/null
  # a job this tunnel attached to, or one kept for others (KEEP_INSTANCE), outlives the tunnel
  if [ "${TUNNEL_ATTACHED:-false}" != true ] && [ "${KEEP_INSTANCE:-false}" != true ]; then
    scancel "$SESSION_ID" 2>/dev/null
  fi
}
trap finish EXIT
trap 'exit 130' HUP INT TERM

while job_active; do
  sleep "${TUNNEL_POLL_INTERVAL:-30}"
done
echo "job $SESSION_ID has ended"
