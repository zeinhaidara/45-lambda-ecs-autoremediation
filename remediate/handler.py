"""ECS auto-remediation: force a new deployment after a crashed task, or scale out
by one when a service's target group has unhealthy targets. Opt-in by tag only."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

log = logging.getLogger()
log.setLevel(logging.INFO)

OPT_IN_TAG = "auto-remediate"
CRASH_STOP_CODES = {"EssentialContainerExited", "TaskFailedToStart"}


@dataclass(frozen=True)
class Config:
    clusters: tuple[str, ...]
    cooldown_minutes: int = 15
    max_desired_count: int = 10
    unhealthy_threshold: int = 1
    dry_run: bool = True

    @classmethod
    def from_env(cls, env=os.environ) -> Config:
        raw = env.get("DRY_RUN", "true").strip().lower()
        if raw not in ("true", "false"):
            raise ValueError(f"DRY_RUN must be true or false, got {raw!r}")
        cfg = cls(
            clusters=tuple(c for c in env.get("CLUSTERS", "").split(",") if c.strip()),
            cooldown_minutes=int(env.get("COOLDOWN_MINUTES", "15")),
            max_desired_count=int(env.get("MAX_DESIRED_COUNT", "10")),
            unhealthy_threshold=int(env.get("UNHEALTHY_THRESHOLD", "1")),
            dry_run=raw == "true",
        )
        if cfg.cooldown_minutes < 1 or cfg.max_desired_count < 1 or cfg.unhealthy_threshold < 1:
            raise ValueError(
                "COOLDOWN_MINUTES, MAX_DESIRED_COUNT, UNHEALTHY_THRESHOLD must be >= 1"
            )
        return cfg


@dataclass(frozen=True)
class Decision:
    act: bool
    reason: str


def guard(service: dict, now: datetime, cfg: Config) -> Decision:
    """Checks shared by both remediations. Pure, so it is easy to test."""
    tags = {t["key"]: t["value"] for t in service.get("tags", [])}
    if tags.get(OPT_IN_TAG) != "true":
        return Decision(False, "not opted in")
    deployments = service.get("deployments", [])
    primary = next((d for d in deployments if d["status"] == "PRIMARY"), None)
    if primary is None:
        return Decision(False, "no primary deployment")
    if len(deployments) > 1 or primary.get("rolloutState") == "IN_PROGRESS":
        return Decision(False, "deployment in progress")
    since = now - primary["updatedAt"]
    if since < timedelta(minutes=cfg.cooldown_minutes):
        return Decision(
            False, f"cooldown: last deployment change {int(since.total_seconds())}s ago"
        )
    return Decision(True, "ok")


@dataclass
class Summary:
    dry_run: bool
    actions: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


def _describe(ecs, cluster: str, names: list[str]) -> list[dict]:
    resp = ecs.describe_services(cluster=cluster, services=names, include=["TAGS"])
    if resp.get("failures"):
        raise RuntimeError(f"describe_services failures: {resp['failures']}")
    return resp["services"]


def on_task_stopped(ecs, detail: dict, now: datetime, cfg: Config, out: Summary) -> None:
    group = detail.get("group", "")
    if detail.get("lastStatus") != "STOPPED" or not group.startswith("service:"):
        out.skipped.append({"reason": "not a stopped service task"})
        return
    if detail.get("stopCode") not in CRASH_STOP_CODES:
        # Deployments, scale-in and manual stops are normal; leave them alone.
        out.skipped.append({"reason": f"stop code {detail.get('stopCode')} is not a crash"})
        return
    cluster, name = detail["clusterArn"], group.split(":", 1)[1]
    [service] = _describe(ecs, cluster, [name])
    decision = guard(service, now, cfg)
    if not decision.act:
        out.skipped.append({"service": name, "reason": decision.reason})
        return
    action = {
        "service": name,
        "cluster": cluster,
        "action": "force-new-deployment",
        "why": detail.get("stoppedReason", ""),
    }
    if not cfg.dry_run:
        ecs.update_service(cluster=cluster, service=name, forceNewDeployment=True)
    out.actions.append(action)


def _list_services(ecs, cluster: str) -> list[str]:
    arns, kwargs = [], {"cluster": cluster}
    while True:
        page = ecs.list_services(**kwargs)
        arns.extend(page.get("serviceArns", []))
        if not page.get("nextToken"):
            return arns
        kwargs["nextToken"] = page["nextToken"]


def sweep(ecs, elbv2, now: datetime, cfg: Config, out: Summary) -> None:
    if not cfg.clusters:
        raise ValueError("CLUSTERS is empty; nothing to sweep")
    for cluster in cfg.clusters:
        arns = _list_services(ecs, cluster)
        for i in range(0, len(arns), 10):  # DescribeServices takes at most 10
            for svc in _describe(ecs, cluster, arns[i : i + 10]):
                _check_service(ecs, elbv2, cluster, svc, now, cfg, out)


def _check_service(ecs, elbv2, cluster, svc, now, cfg, out) -> None:
    name = svc["serviceName"]
    tgs = [lb["targetGroupArn"] for lb in svc.get("loadBalancers", []) if "targetGroupArn" in lb]
    if not tgs:
        return
    unhealthy = 0
    for tg in tgs:
        health = elbv2.describe_target_health(TargetGroupArn=tg)["TargetHealthDescriptions"]
        unhealthy += sum(1 for h in health if h["TargetHealth"]["State"] == "unhealthy")
    if unhealthy < cfg.unhealthy_threshold:
        return
    # Tasks from an earlier change are still starting: adding more won't help yet.
    # Uses live task counts only, not deployment timestamps.
    running, pending, desired = svc["runningCount"], svc["pendingCount"], svc["desiredCount"]
    if pending > 0 or running < desired:
        out.skipped.append(
            {
                "service": name,
                "reason": f"tasks still starting: running {running}/{desired}, pending {pending}",
            }
        )
        return
    decision = guard(svc, now, cfg)
    if not decision.act:
        out.skipped.append({"service": name, "reason": decision.reason})
        return
    current = svc["desiredCount"]
    if current >= cfg.max_desired_count:
        out.skipped.append({"service": name, "reason": f"at max desired count {current}"})
        return
    target = current + 1
    if not cfg.dry_run:
        ecs.update_service(cluster=cluster, service=name, desiredCount=target)
    out.actions.append(
        {
            "service": name,
            "cluster": cluster,
            "action": "scale-out",
            "from": current,
            "to": target,
            "unhealthy_targets": unhealthy,
        }
    )


def handler(event, context, ecs=None, elbv2=None, now=None):
    cfg = Config.from_env()
    if ecs is None or elbv2 is None:
        import boto3

        ecs = ecs or boto3.client("ecs")
        elbv2 = elbv2 or boto3.client("elbv2")
    now = now or datetime.now(UTC)
    out = Summary(dry_run=cfg.dry_run)

    kind = event.get("detail-type")
    if kind == "ECS Task State Change":
        on_task_stopped(ecs, event["detail"], now, cfg, out)
    elif kind == "Scheduled Event":
        sweep(ecs, elbv2, now, cfg, out)
    else:
        raise ValueError(f"unsupported event detail-type {kind!r}")

    log.info(json.dumps(out.__dict__, default=str))
    return out.__dict__
