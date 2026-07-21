# AWS/CloudWatch telemetry — what works, what doesn't

Split out of NR_NOTES.md on 2026-07-18 (findings originally appended there;
hb / HungerBox account 356367897942, ap-south-1). The CloudWatch query CLI
does not exist yet — these notes predate it and will guide its shape.

## Append bar — read before adding anything

Same bar as NR_NOTES.md: instrument facts only — access paths, metric
emission quirks, queries/permissions that fail and why. Test: *the entry is
true regardless of which incident happens next.* No incident conclusions,
no "X usually breaks".

## Access

- **AWS CLI profile `hb-role`** assumes `ingren-rca-readonly` — CloudWatch metrics,
  alarm history, and `elasticloadbalancing:Describe*` all work. The stale `hungerbox`
  profile was deleted 2026-07-16.
- **ALB access logs ARE enabled** for `app/hb-prod-lb` → `s3://hb-prod-alb-logs`
  (no prefix), but `ingren-rca-readonly` has **no S3 read** on the bucket
  (AccessDenied on `s3:ListBucket`). Policy addition requested from devops.

## Boundary with New Relic

- **NR does NOT ingest ALB/AWS integration data** — no `AwsAlbSample`-style event
  types, and NR `Log` only carries host-level files (nginx access/error, syslog,
  auth, redis). ELB-generated errors are invisible in NR; go to CloudWatch/S3.

## Metric emission quirks

- **CloudWatch alarm `hb-prod-lb-high-ELB-5XX`** rests in INSUFFICIENT_DATA, not OK:
  `HTTPCode_ELB_503_Count` only emits datapoints when 503s occur and the alarm
  treats missing as missing. Don't read INSUFFICIENT_DATA transitions as outages.
- Empty target groups emit **no HealthyHostCount datapoints at all** — absence of
  the metric (SampleCount 0) is how you detect "no targets registered", and its
  history tells you since when.
- This generalizes beyond `HealthyHostCount`: any Sum-based `AWS/ApplicationELB`
  count metric (`RejectedConnectionCount`, `TargetConnectionErrorCount`, etc.)
  returns an **empty `Datapoints` array**, not zero-valued datapoints, for
  periods with no occurrences. Zero occurrences and "metric doesn't exist for
  this dimension" look identical in `get-metric-statistics` output — check
  `describe-target-groups` / `describe-alarms` if you need to tell them apart.
- `get-metric-statistics --statistics` only accepts `SampleCount`/`Average`/
  `Sum`/`Minimum`/`Maximum` — percentiles (`p99` etc.) are rejected
  (`InvalidParameterValue`) and require the separate `--extended-statistics`
  flag instead.

## Identifiers and dimension rules

- Per-target-group metrics need both the `LoadBalancer` and `TargetGroup`
  dimensions together; discover current target groups at investigation time
  via `elbv2 describe-target-groups --load-balancer-arn <lb-arn>` (topology
  is mutable — never trust a cached inventory).
- LB ARN for `app/hb-prod-lb`:
  `arn:aws:elasticloadbalancing:ap-south-1:356367897942:loadbalancer/app/hb-prod-lb/a0ccd5f577529509`

## Decomposing an aggregate 5xx/4xx alarm

- `AWS/ApplicationELB` exposes per-status-code metrics beyond the aggregate
  `HTTPCode_ELB_5XX_Count` / `HTTPCode_Target_5XX_Count`: `HTTPCode_ELB_500_Count`,
  `HTTPCode_ELB_502_Count`, `HTTPCode_ELB_503_Count`, `HTTPCode_ELB_504_Count`
  each exist as their own metric (LB dimension, and per-AZ). Don't assume which
  ones are emitted for a given LB — run `cloudwatch list-metrics --namespace
  AWS/ApplicationELB --dimensions Name=LoadBalancer,Value=<lb>` first and query
  only the codes that actually appear; codes with zero occurrences in the
  window return an **empty `Datapoints` array**, same empty-vs-nonexistent
  ambiguity as other Sum-based ELB count metrics noted above.
- Querying an `HTTPCode_ELB_*_Count` metric **with the `TargetGroup` dimension**
  attributes the response to that group only if the LB actually reached a
  routing decision for it. An LB-level 5xx that returns **empty** for every
  registered target group (queried individually) means the error was generated
  by the ALB frontend before/without completing target routing — a way to tell
  frontend-side errors apart from target-side ones using CloudWatch alone, no
  access-log needed.
- `HealthyStateRouting` / `UnhealthyStateRouting` (dimensions: `LoadBalancer` +
  `TargetGroup` + `AvailabilityZone`) are newer, per-AZ health metrics, finer
  grained than the classic `HealthyHostCount`/`UnHealthyHostCount` (which is a
  per-TG total, not per-AZ, and can mask a single-AZ gap). Discover their
  exact AZ dimension values via `list-metrics` rather than guessing.
- `ConsumedLCUs` (namespace `AWS/ApplicationELB`, `LoadBalancer` dimension
  only) is the LB's own capacity-unit consumption — check this before
  suspecting LB-side throttling; typical idle/light values are ~0.005–0.02.
- `elbv2 describe-listeners --load-balancer-arn <arn>` returns each listener's
  `DefaultActions[].ForwardConfig.TargetGroups[]` with a `Weight` per group.
  An LB can have target groups registered (visible in
  `describe-target-groups`) that carry **zero listener weight or aren't
  referenced by any listener rule at all** — i.e. exist but serve no live
  traffic. Always check listener weights before assuming a target group in
  `describe-target-groups` is part of the live serving path.
- `cloudwatch describe-alarm-history --alarm-name <name> --history-item-type
  StateUpdate --start-date <x> --end-date <y>` is the way to check whether an
  alarm is chronically flapping or fired once — cheap and worth running before
  investigating a specific breach, to calibrate whether the alarm itself is
  noisy.
- `AWS/EC2` `StatusCheckFailed` (dimension `InstanceId`) is the standard
  instance-level health metric for a reboot/network-blip check when a target
  group has few enough hosts to check them individually.
