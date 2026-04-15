# Trace: DeploymentAgent — allinterviews S3_NO_SUCH_KEY
**Date:** 2026-04-15 14:29–14:30 CDT  
**Trace ID:** 78b129d173288601  
**Duration:** 35.7s | **Iterations:** 8

---

## Issues Found

### 1. Wrong ECS cluster name — `ClusterNotFoundException`
- Tool called `get_ecs_status(cluster="default", service="allinterviews")` — twice
- AWS returned: `ClusterNotFoundException` — cluster "default" doesn't exist
- **Root cause:** DeploymentAgent is guessing the cluster name instead of knowing the real one

### 2. CloudWatch Logs — `AccessDeniedException`
- Tool called `get_service_logs(log_group="/aws/ecs/allinterviews", minutes=30)`
- AWS returned: `AccessDeniedException` — IAM user `AgentPlatformMonitor` lacks `logs:DescribeLogStreams`
- **Root cause:** Missing IAM permission

### 3. No ECS CPU/memory metrics
- Tool called `get_metrics(namespace="AWS/ECS", metric_name="CPUUtilization", dimensions={ServiceName: "allinterviews"})`
- Returned 0 data points — service not found under that name in CloudWatch

### 4. Wrong ALB names — tried 3 guesses, all 404
- Tried: `allinterviews`, `allinterviews-alb`, `prod-allinterviews` — all `LoadBalancerNotFound`
- **Root cause:** Agent doesn't know the actual ALB name for this service

---

## Fixes Needed

| # | Fix | File |
|---|-----|------|
| 1 | Pass real ECS cluster name via config or error event metadata | `app/core/config.py` or `ErrorEvent.metadata` |
| 2 | Add `logs:DescribeLogStreams` + `logs:GetLogEvents` to `AgentPlatformMonitor` IAM policy | AWS IAM |
| 3 | Pass cluster name in CloudWatch dimensions: `{ClusterName: X, ServiceName: Y}` | `app/agents/deployment.py` |
| 4 | Pass ALB name via config or look it up dynamically from ECS service tags | `app/agents/deployment.py` |
