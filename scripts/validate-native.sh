#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
umask 077
mkdir -p validation-logs
record="$PWD/validation-logs/times.jsonl"
attempt=${1:-1}
image=mediaops-validation:cleanup-20261017-$attempt
sh scripts/run-phase.sh "$record" "build-$attempt" "$PWD/validation-logs/build-$attempt.log" -- docker build --network host --platform linux/amd64 -t "$image" .
[ "$(docker image inspect "$image" --format '{{.Architecture}}')" = amd64 ]
sh scripts/run-phase.sh "$record" "tests-$attempt" "$PWD/validation-logs/tests-$attempt.log" -- docker run --rm --network none --read-only --tmpfs /tmp -e DATABASE_URL=sqlite:// -e PYTHONWARNINGS=ignore -e CONFIG_FILE=/tmp/no-config --entrypoint python -v "$PWD/tests:/app/tests:ro" "$image" -m unittest discover -s tests -v
sh scripts/run-phase.sh "$record" "smoke-$attempt" "$PWD/validation-logs/smoke-$attempt.log" -- docker run --rm --network none --read-only --tmpfs /tmp -e DATABASE_URL=sqlite:// -e CONFIG_FILE=/tmp/no-config --entrypoint python "$image" -c 'from fastapi.testclient import TestClient; from app.main import app; c=TestClient(app); assert c.get("/healthz").json()=={"ok":True}; assert c.get("/static/libraries.js").status_code==200; print("Exact image health and assets passed")'
docker image inspect "$image" --format '{{.Architecture}} {{.Id}}' > validation-logs/image.txt
cat "$record"
cat validation-logs/image.txt
