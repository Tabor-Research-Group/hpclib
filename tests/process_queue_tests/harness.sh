#!/bin/bash
# Minimal test harness shared by the job_queue bash tests - no external
# framework (bats, shunit2, ...) required, matching hpclib's own
# dependency-light style.
set -uo pipefail

TESTS_RUN=0
TESTS_FAILED=0

function test_case {
  TESTS_RUN=$((TESTS_RUN+1))
  echo "--- $1 ---"
}

function pass { echo "  PASS: $1"; }
function fail { echo "  FAIL: $1" >&2; TESTS_FAILED=$((TESTS_FAILED+1)); }

function assert_eq {
  local actual="$1" expected="$2" msg="$3"
  if [ "$actual" = "$expected" ]; then pass "$msg (got '$actual')"
  else fail "$msg: expected '$expected', got '$actual'"; fi
}

function assert_count {
  local actual="$1" expected="$2" msg="$3"
  if [ "$actual" -eq "$expected" ]; then pass "$msg ($actual)"
  else fail "$msg: expected count $expected, got $actual"; fi
}

function assert_file_exists {
  local path="$1" msg="${2:-$1 exists}"
  if [ -f "$path" ]; then pass "$msg"; else fail "$msg: no such file: $path"; fi
}

function harness_summary {
  echo
  echo "================================================================"
  echo "  $TESTS_RUN test case(s), $TESTS_FAILED failure(s)"
  echo "================================================================"
  [ "$TESTS_FAILED" -eq 0 ]
}
