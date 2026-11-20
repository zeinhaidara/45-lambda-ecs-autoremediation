import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from remediate.handler import Config, guard, handler

ROOT = Path(__file__).parent.parent
NOW = datetime(2026, 11, 20, 10, 20, tzinfo=UTC)
CLUSTER_ARN = "arn:aws:ecs:eu-west-2:123456789012:cluster/shop"
TG = "arn:aws:elasticloadbalancing:eu-west-2:123456789012:targetgroup/checkout/0123456789abcdef"


def event(name):
    return json.loads((ROOT / "events" / f"{name}.json").read_text())


def service(
    name="checkout",
    opted_in=True,
    desired=2,
    deployed_minutes_ago=60,
    in_progress=False,
    deployments=1,
    tgs=(TG,),
    running=None,
    pending=0,
):
    primary = {
        "status": "PRIMARY",
        "rolloutState": "IN_PROGRESS" if in_progress else "COMPLETED",
        "updatedAt": NOW - timedelta(minutes=deployed_minutes_ago),
    }
    deps = [primary] + [
        {"status": "ACTIVE", "rolloutState": "COMPLETED", "updatedAt": NOW - timedelta(hours=2)}
    ] * (deployments - 1)
    return {
        "serviceName": name,
        "serviceArn": f"arn:aws:ecs:eu-west-2:123456789012:service/shop/{name}",
        "desiredCount": desired,
        "runningCount": desired if running is None else running,
        "pendingCount": pending,
        "deployments": deps,
        "loadBalancers": [
            {"targetGroupArn": t, "containerName": "app", "containerPort": 8080} for t in tgs
        ],
        "tags": [{"key": "auto-remediate", "value": "true"}] if opted_in else [],
    }


class FakeECS:
    def __init__(self, services, page_size=2):
        self.services = {s["serviceName"]: s for s in services}
        self.page_size = page_size
        self.updates, self.list_tokens, self.describe_sizes = [], [], []

    def list_services(self, cluster, nextToken=None):
        self.list_tokens.append(nextToken)
        arns = [s["serviceArn"] for s in self.services.values()]
        start = int(nextToken or 0)
        page = {"serviceArns": arns[start : start + self.page_size]}
        if start + self.page_size < len(arns):
            page["nextToken"] = str(start + self.page_size)
        return page

    def describe_services(self, cluster, services, include):
        assert include == ["TAGS"]
        assert len(services) <= 10
        self.describe_sizes.append(len(services))
        names = [s.rsplit("/", 1)[-1] for s in services]
        return {"services": [copy.deepcopy(self.services[n]) for n in names], "failures": []}

    def update_service(self, **kw):
        self.updates.append(kw)


class FakeELB:
    def __init__(self, states):
        self.states = states  # target group arn -> list of states

    def describe_target_health(self, TargetGroupArn):
        return {
            "TargetHealthDescriptions": [
                {"Target": {"Id": f"10.0.0.{i}"}, "TargetHealth": {"State": s}}
                for i, s in enumerate(self.states.get(TargetGroupArn, []))
            ]
        }


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("CLUSTERS", "shop")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("MAX_DESIRED_COUNT", "4")
    monkeypatch.setenv("COOLDOWN_MINUTES", "15")


def run(evt, ecs, elb=None):
    return handler(evt, None, ecs=ecs, elbv2=elb or FakeELB({}), now=NOW)


# --- guard ----------------------------------------------------------------------------

CFG = Config(clusters=("shop",))


@pytest.mark.parametrize(
    ("svc", "reason"),
    [
        (service(opted_in=False), "not opted in"),
        (service(in_progress=True), "deployment in progress"),
        (service(deployments=2), "deployment in progress"),
        (service(deployed_minutes_ago=5), "cooldown"),
    ],
)
def test_guard_refuses(svc, reason):
    d = guard(svc, NOW, CFG)
    assert not d.act and reason in d.reason


def test_guard_allows_settled_opted_in_service():
    assert guard(service(), NOW, CFG).act


# --- crashed task -> forced deployment ----------------------------------------------------


def test_crashed_task_forces_new_deployment(env):
    ecs = FakeECS([service()])
    out = run(event("task-stopped"), ecs)
    assert ecs.updates == [
        {"cluster": CLUSTER_ARN, "service": "checkout", "forceNewDeployment": True}
    ]
    assert out["actions"][0]["action"] == "force-new-deployment"


@pytest.mark.parametrize("code", ["ServiceSchedulerInitiated", "UserInitiated", "SpotInterruption"])
def test_normal_stops_are_ignored(env, code):
    evt = event("task-stopped")
    evt["detail"]["stopCode"] = code
    ecs = FakeECS([service()])
    out = run(evt, ecs)
    assert ecs.updates == []
    assert "not a crash" in out["skipped"][0]["reason"]


def test_standalone_task_is_ignored(env):
    evt = event("task-stopped")
    evt["detail"]["group"] = "family:batch-job"
    ecs = FakeECS([service()])
    run(evt, ecs)
    assert ecs.updates == []


def test_untagged_service_is_never_touched(env):
    ecs = FakeECS([service(opted_in=False)])
    out = run(event("task-stopped"), ecs)
    assert ecs.updates == []
    assert out["skipped"][0]["reason"] == "not opted in"


def test_crash_during_deployment_is_left_alone(env):
    ecs = FakeECS([service(in_progress=True)])
    run(event("task-stopped"), ecs)
    assert ecs.updates == []


# --- sweep -> bounded scale-out ------------------------------------------------------------


def test_unhealthy_targets_scale_out_by_one(env):
    ecs = FakeECS([service(desired=2)])
    out = run(event("scheduled-sweep"), ecs, FakeELB({TG: ["healthy", "unhealthy"]}))
    assert ecs.updates == [{"cluster": "shop", "service": "checkout", "desiredCount": 3}]
    assert out["actions"][0]["unhealthy_targets"] == 1


def test_scale_out_stops_at_hard_max(env):
    ecs = FakeECS([service(desired=4)])
    out = run(event("scheduled-sweep"), ecs, FakeELB({TG: ["unhealthy"]}))
    assert ecs.updates == []
    assert "max desired count" in out["skipped"][0]["reason"]


def test_healthy_services_are_left_alone(env):
    ecs = FakeECS([service()])
    out = run(event("scheduled-sweep"), ecs, FakeELB({TG: ["healthy", "healthy", "draining"]}))
    assert ecs.updates == [] and out["actions"] == []


def test_cooldown_blocks_repeated_scale_out(env):
    ecs = FakeECS([service(deployed_minutes_ago=3)])
    out = run(event("scheduled-sweep"), ecs, FakeELB({TG: ["unhealthy"]}))
    assert ecs.updates == []
    assert out["skipped"][0]["reason"].startswith("cooldown")


def test_sweep_pages_and_batches_services(env):
    services = [service(name=f"svc{i}", opted_in=False) for i in range(23)]
    ecs = FakeECS(services, page_size=5)
    run(event("scheduled-sweep"), ecs, FakeELB({}))
    assert ecs.list_tokens == [None, "5", "10", "15", "20"]
    assert ecs.describe_sizes == [10, 10, 3]


def test_unhealthy_untagged_service_is_never_scaled(env):
    ecs = FakeECS([service(opted_in=False)])
    run(event("scheduled-sweep"), ecs, FakeELB({TG: ["unhealthy"] * 5}))
    assert ecs.updates == []


# --- dry run, config, errors ------------------------------------------------------------------


def test_dry_run_is_the_default(monkeypatch):
    monkeypatch.setenv("CLUSTERS", "shop")
    monkeypatch.delenv("DRY_RUN", raising=False)
    ecs = FakeECS([service()])
    out = run(event("task-stopped"), ecs)
    assert out["dry_run"] is True
    assert out["actions"] and ecs.updates == []


@pytest.mark.parametrize(
    ("var", "value"), [("DRY_RUN", "maybe"), ("MAX_DESIRED_COUNT", "0"), ("COOLDOWN_MINUTES", "0")]
)
def test_bad_config_fails(monkeypatch, var, value):
    monkeypatch.setenv("CLUSTERS", "shop")
    monkeypatch.setenv(var, value)
    with pytest.raises(ValueError):
        run(event("task-stopped"), FakeECS([service()]))


def test_sweep_without_clusters_fails(monkeypatch):
    monkeypatch.setenv("CLUSTERS", "")
    with pytest.raises(ValueError, match="CLUSTERS"):
        run(event("scheduled-sweep"), FakeECS([]))


def test_unknown_event_fails(env):
    with pytest.raises(ValueError, match="unsupported"):
        run({"detail-type": "EC2 Instance State-change Notification"}, FakeECS([]))


def test_describe_failures_are_raised(env):
    class Broken(FakeECS):
        def describe_services(self, **kw):
            return {"services": [], "failures": [{"arn": "x", "reason": "MISSING"}]}

    with pytest.raises(RuntimeError, match="MISSING"):
        run(event("task-stopped"), Broken([service()]))


def test_iam_policy_limits_updates_to_opted_in_services():
    policy = json.loads((ROOT / "iam" / "policy.json").read_text())
    [update] = [s for s in policy["Statement"] if s["Action"] == "ecs:UpdateService"]
    assert update["Condition"] == {"StringEquals": {"aws:ResourceTag/auto-remediate": "true"}}
    actions = [
        a
        for s in policy["Statement"]
        for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
    ]
    assert not any("*" in a for a in actions)


# --- tasks still starting --------------------------------------------------------------


def test_no_scale_out_while_tasks_are_pending(env):
    # Last deployment change was long ago, so only the task counts can stop this.
    ecs = FakeECS([service(desired=2, running=2, pending=1, deployed_minutes_ago=600)])
    out = run(event("scheduled-sweep"), ecs, FakeELB({TG: ["unhealthy"]}))
    assert ecs.updates == []
    assert out["skipped"][0]["reason"] == "tasks still starting: running 2/2, pending 1"


def test_no_scale_out_while_running_is_below_desired(env):
    ecs = FakeECS([service(desired=3, running=2, pending=0, deployed_minutes_ago=600)])
    out = run(event("scheduled-sweep"), ecs, FakeELB({TG: ["unhealthy"]}))
    assert ecs.updates == []
    assert out["skipped"][0]["reason"] == "tasks still starting: running 2/3, pending 0"


def test_scale_out_resumes_once_tasks_have_settled(env):
    ecs = FakeECS([service(desired=3, running=3, pending=0, deployed_minutes_ago=600)])
    run(event("scheduled-sweep"), ecs, FakeELB({TG: ["unhealthy"]}))
    assert ecs.updates == [{"cluster": "shop", "service": "checkout", "desiredCount": 4}]


def test_iam_condition_key_is_one_update_service_supports():
    # Per the AWS service authorization reference for ECS, UpdateService on the
    # service resource supports aws:ResourceTag/${TagKey} and ecs:ResourceTag/${TagKey}.
    supported = {"aws:ResourceTag/", "ecs:ResourceTag/"}
    policy = json.loads((ROOT / "iam" / "policy.json").read_text())
    [update] = [s for s in policy["Statement"] if s["Action"] == "ecs:UpdateService"]
    for key in update["Condition"]["StringEquals"]:
        assert any(key.startswith(prefix) for prefix in supported), key
    assert update["Resource"].endswith(":service/*/*")  # long ARN format, required for tags
