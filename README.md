# Mini-ERP: the whale customer

This is a small demo of a cloud business system (an "ERP") used in the exercise
*Refinement of the Whale*. You do not need to read or understand any code to use it.

## The business situation

You run a cloud ERP for small businesses. Every customer (called a *tenant*) has 5 to 20
users who enter orders and look at reports.

- **300 small customers** pay about €99 a month each.
- All of them share **one system and one database**. This keeps costs low.
- A distributor with **1,200 users** wants to join. They would pay **€6,000 a month**
  on a 12-month contract. We call this big customer **the whale**.
- They require **99.9% availability** (a promise that the system works almost all the time)
  and **separation of their data** from everyone else's.

The catch: the system was never built for a customer this big. In a test it had to handle
about 8 times today's peak load, and the database grew by 35%. Worst of all, the whale's
**month-end reports** slowed the test system down for everybody else.

Sales needs a go / no-go decision within a week. The question for the exercise:
**can we take on the whale without hurting our 300 small customers, and what would it cost?**

## Run and test in GitHub Codespaces

On the `dedicated-erp` branch, open **Code → Codespaces** and create a Codespace. Wait for setup to install PostgreSQL and the Python dependencies. In its terminal run `bash codespaces-verify.sh`.

The script seeds two disposable PostgreSQL clusters, launches two API processes, checks tenant access, runs the queue tests, and compares normal traffic with a month-end report burst. It writes `results/github-codespaces.json` and stops its processes. Set `ERP_TEST_PHASE_SECONDS` to change the phase duration.

For the interactive demo, run `bash codespaces-verify.sh --serve`. Open ports 8000 (shared) and 8001 (distributor) from **Ports**. Keep both private. Use username `customer` and password `shared-fixture` or `dedicated-fixture` respectively. For distributor Operations at `/ops`, use `ops` and `dedicated-ops-fixture`. Stop the command with Ctrl+C and stop the Codespace when finished.

The script pins each API and its PostgreSQL cluster to a separate logical CPU using taskset. It needs at least two available logical CPUs. The demo uses synthetic data and the stacks still share one cloud host. Separate-host staging with the real workload is required before signing an SLA.

## Codespaces test evidence

The two cloud runs passed the tenant smoke checks, both report-queue unit tests, and the 800 ms target for normal distributor requests. The 10% small-tenant p95 gate failed on the two-CPU Codespace, both with shared CPU scheduling (34.02 to 93.61 ms) and with each stack pinned to a CPU (18.02 to 171.70 ms). Raw results are in `results/github-codespaces-shared-cpu.json` and `results/github-codespaces.json`. The same-host result cannot confirm the separate-host acceptance criterion.

The interactive smoke test also verified both Customer pages and distributor Operations. The simulation started and stopped, and a shared order read and distributor report succeeded while it ran.

## Month-end report protection

The distributor API queues at most 24 report requests per process, including the
one running. It runs one report query at a time and waits at least 150 ms
between query starts. Waiting requests do not take a PostgreSQL connection or a
worker thread. When the queue is full, the API returns HTTP 429 with
`Retry-After: 1` and the customer page prompts the user to retry. Queued reports
still query live data when they run. Order reads and writes bypass the queue.
The Operations simulation defaults to eight report clients.

`ERP_REPORT_QUEUE_SIZE` and `ERP_REPORT_INTERVAL_SECONDS` set the bounds.
The default interval permits up to about 6.7 report starts per second per
process. Tune the bounds against the expected report volume and report wait
times. A staging deployment with multiple API replicas must account for the
sum of their report budgets. The load test records rejected reports as errors.
Run the queue regression test with `python -m unittest discover -s tests -v`.

## Separate-host staging

Run each API and its PostgreSQL instance on a separate authorized host, with independent storage and credentials. Run the load generator from a third host with observed peak traffic.

## Availability assessment

This demo has one API process and one database instance per environment. Use
`/health` for process liveness and `/ready` for database readiness. The 99.9%
target allows 43.2 minutes of unavailability in a 30-day month.

- Size the distributor environment on dedicated capacity. Put at least two API
  replicas behind a health-checked load balancer across availability zones, with
  rolling releases and a tested rollback. Use managed PostgreSQL with
  multi-zone failover, automated backups and point-in-time recovery.
- Probe a representative authenticated order transaction externally every
  minute and track unavailable minutes. Alert the on-call operator on failed
  probes, elevated error rates, saturation, replication lag and backup failure.
  Track maintenance against the budget unless the contract expressly excludes it.
- Before committing, exercise API loss, database failover and restoration using
  production-like data. Target under five minutes for automatic failover and
  verify recovery point and restore time with an actual restore. Agree on the
  data-loss target with the customer and document how a prolonged outage would
  affect the monthly budget. Run repeatable peak-load and month-end tests on
  separate hosts at the expected 1,200-user traffic mix and data volume.
- Obtain quotes for dedicated compute, database HA, backups, monitoring and
  on-call operations. The repository cost model illustrates €4,300 per month
  for the dedicated option and €1,700 per month in contribution at €6,000.
  These figures are assumptions that need pricing and operating validation.

Proceed with the assessment. Make the contract decision after validating tenant
authorization, load isolation, recovery and cost in representative staging.
