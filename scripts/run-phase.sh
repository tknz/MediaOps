#!/bin/sh
set -u
record=$1; phase=$2; output=$3; shift 3
[ "${1:-}" = -- ] && shift
start=$(date +%s)
"$@" >"$output" 2>&1
status=$?
end=$(date +%s)
printf '{"phase":"%s","start":%s,"end":%s,"elapsed_seconds":%s,"exit_code":%s}\n' "$phase" "$start" "$end" "$((end-start))" "$status" >>"$record"
exit "$status"
