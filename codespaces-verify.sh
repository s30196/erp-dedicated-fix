#!/usr/bin/env bash
# Run from the repository root in a disposable GitHub Codespace.
set -euo pipefail

pg_bin=$(pg_config --bindir)
test_dir=$(mktemp -d)
pg_started=()
api_pids=()
cleanup() {
  for pid in "${api_pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  for cluster in "${pg_started[@]}"; do
    "$pg_bin/pg_ctl" -D "$test_dir/$cluster" -m fast -w stop >/dev/null || true
  done
}
trap cleanup EXIT

start_stack() {
  local scope=$1 db_port=$2 api_port=$3 password=$4
  local dsn="postgresql://erp@127.0.0.1:$db_port/postgres"
  "$pg_bin/initdb" --no-sync -D "$test_dir/$scope" -U erp -A trust --locale=C --encoding=UTF8 >/dev/null
  "$pg_bin/pg_ctl" -D "$test_dir/$scope" -l "$test_dir/$scope-db.log" \
    -o "-h 127.0.0.1 -p $db_port -k $test_dir" -w start >/dev/null
  pg_started+=("$scope")
  psql "$dsn" -v ON_ERROR_STOP=1 -f db/init.sql -f "db/$scope.sql" >/dev/null
  ERP_SCOPE="$scope" DATABASE_URL="$dsn" python -m app.seed
  ERP_SCOPE="$scope" DATABASE_URL="$dsn" ERP_CUSTOMER_PASSWORD="$password" POOL_TIMEOUT=2 \
    python -m uvicorn app.main:app --port "$api_port" --no-access-log >"$test_dir/$scope-api.log" 2>&1 &
  api_pids+=("$!")
  for _ in {1..40}; do
    if curl -fs "http://127.0.0.1:$api_port/ready" >/dev/null; then return; fi
    sleep .25
  done
  cat "$test_dir/$scope-api.log"
  return 1
}

python -m unittest discover -s tests -v
start_stack shared 15432 8000 shared-fixture
start_stack dedicated 15433 8001 dedicated-fixture
python - <<'PY'
import httpx

shared, dedicated = "http://127.0.0.1:8000", "http://127.0.0.1:8001"
body = {"lines": [{"product": "check", "qty": 1, "unit_price": "1.00"}]}
for url, password, wrong_password, visible, blocked in (
    (shared, "shared-fixture", "dedicated-fixture", 2, 1),
    (dedicated, "dedicated-fixture", "shared-fixture", 1, 2),
):
    good = ("customer", password)
    assert httpx.get(f"{url}/t/{visible}/orders", auth=("customer", wrong_password)).status_code == 401
    assert httpx.get(f"{url}/t/{visible}/orders", auth=good).status_code == 200
    assert httpx.get(f"{url}/t/{blocked}/orders", auth=good).status_code == 404
    assert httpx.post(f"{url}/t/{blocked}/orders", auth=good, json=body).status_code == 404
    assert httpx.post(f"{url}/t/{visible}/orders", auth=good, json=body).status_code == 201
print("Codespaces tenant and credentials smoke checks passed.")
PY
ERP_SHARED_PASSWORD=shared-fixture ERP_WHALE_PASSWORD=dedicated-fixture \
  python loadtest/loadtest.py --base-url http://127.0.0.1:8000 --whale-url http://127.0.0.1:8001 \
  --duration "${ERP_TEST_PHASE_SECONDS:-30}" --label github-codespaces
