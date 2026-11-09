"""One-shot parking transition; delete after application and CDK adoption.

Uses the deployed templates so unrelated, still-unadopted main-branch changes
cannot replace task images, credentials, certificates, DNS or stored data.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
from pathlib import Path

ACCOUNT = "877352799272"
REGION = "us-east-1"
SERVICE = "ServiceD69D759B"
REMOVE_SERVICES = {
    "ServiceTaskCountTarget23E25614": "AWS::ApplicationAutoScaling::ScalableTarget",
    "ServiceTaskCountTargetCpuScalingCC8A5DF6": "AWS::ApplicationAutoScaling::ScalingPolicy",
    "BackendTaskMissing880E60D0": "AWS::CloudWatch::Alarm",
    "GameServerTaskMissing700F1DE5": "AWS::CloudWatch::Alarm",
    "SpriteWorkerTaskMissingF3C7A71F": "AWS::CloudWatch::Alarm",
}
REMOVE_NETWORK = {
    "VpcpublicSubnet1NATGatewayA036E8A6": "AWS::EC2::NatGateway",
    "VpcpublicSubnet1EIP411541E6": "AWS::EC2::EIP",
    "VpcprivateSubnet1DefaultRouteB506891A": "AWS::EC2::Route",
    "VpcprivateSubnet2DefaultRouteBAC3C1C3": "AWS::EC2::Route",
    "NatPortErrorAlarmCC295C98": "AWS::CloudWatch::Alarm",
}


def encoded(template):
    return json.dumps(template, sort_keys=True, separators=(",", ":"))


def digest(template):
    return hashlib.sha256(encoded(template).encode()).hexdigest()


def transform(stack, template):
    """Only fixed logical IDs may disappear; only ECS DesiredCount may change."""
    if not re.fullmatch(r"goosetown-(dev|prod)-(compute|gameserver|sprite-worker|worldgm|network)", stack):
        raise ValueError("Stack is outside parking scope")
    result = copy.deepcopy(template)
    resources = result["Resources"]
    network = stack.endswith("-network")
    removals = REMOVE_NETWORK if network else REMOVE_SERVICES
    changed = []
    if not network:
        services = [k for k, v in resources.items() if v["Type"] == "AWS::ECS::Service"]
        if services != [SERVICE]:
            raise ValueError("Unexpected service logical identity")
        if resources[SERVICE]["Properties"].get("DesiredCount") != 0:
            resources[SERVICE]["Properties"]["DesiredCount"] = 0
            changed.append({"id": SERVICE, "action": "Modify", "field": "DesiredCount", "value": 0})
    removed = set()
    for logical_id, resource_type in removals.items():
        if logical_id not in resources:
            continue
        if resources[logical_id]["Type"] != resource_type:
            raise ValueError("Resource logical identity has changed")
        del resources[logical_id]
        removed.add(logical_id)
        changed.append({"id": logical_id, "action": "Remove", "type": resource_type})
    for resource in resources.values():
        deps = resource.get("DependsOn")
        if deps:
            deps = [deps] if isinstance(deps, str) else deps
            kept = [d for d in deps if d not in removed]
            if kept:
                resource["DependsOn"] = kept
            else:
                resource.pop("DependsOn")

    def references(node):
        if isinstance(node, dict):
            att = node.get("Fn::GetAtt")
            referenced = att[0] if isinstance(att, list) else att.split(".")[0] if isinstance(att, str) else None
            sub = node.get("Fn::Sub")
            sub = sub[0] if isinstance(sub, list) else sub
            sub_ref = isinstance(sub, str) and any(
                re.search(r"\$\{" + re.escape(key) + r"(?:\.|\})", sub) for key in removed
            )
            if node.get("Ref") in removed or referenced in removed or sub_ref:
                raise ValueError("Retirement would leave a dangling resource reference")
            for value in node.values():
                references(value)
        elif isinstance(node, list):
            for value in node:
                references(value)

    references(result)
    return result, changed


def deployed(cf, stack, changeset=None):
    args = {"StackName": stack, "TemplateStage": "Original"}
    if changeset:
        args["ChangeSetName"] = changeset
    template = cf.get_template(**args)["TemplateBody"]
    return json.loads(template) if isinstance(template, str) else template


def parked(ecs, scaling, environment):
    """Before NAT retirement, both service count and independent task count must be zero."""
    cluster = f"goosetown-{environment}-shared"
    arns = []
    for page in ecs.get_paginator("list_services").paginate(cluster=cluster):
        arns.extend(page["serviceArns"])
    if len(arns) != 4:
        raise ValueError("Unexpected legacy cluster service count")
    services = ecs.describe_services(cluster=cluster, services=arns)
    if services.get("failures") or any(
        s["desiredCount"] or s["runningCount"] or s["pendingCount"] for s in services["services"]
    ):
        raise ValueError("Stop all legacy services before retiring NAT")
    for status in ("RUNNING", "PENDING"):
        if ecs.list_tasks(cluster=cluster, desiredStatus=status)["taskArns"]:
            raise ValueError("A standalone legacy task still needs network access")
    ids = [f"service/{cluster}/{s['serviceName']}" for s in services["services"]]
    if scaling.describe_scalable_targets(ServiceNamespace="ecs", ResourceIds=ids)["ScalableTargets"]:
        raise ValueError("Remove legacy autoscaling before retiring NAT")


def unchanged_parameters(template, existing, details):
    # Template equality alone is insufficient: a changed parameter can replace an
    # image or credential without changing a single byte of the template.
    if any(p.get("NoEcho") for p in template.get("Parameters", {}).values()):
        raise ValueError("Opaque parameters require separate review; parking refuses them")

    def values(rows):
        return {p["ParameterKey"]: p.get("ParameterValue") for p in rows}

    if values(existing.get("Parameters", [])) != values(details.get("Parameters", [])):
        raise ValueError("Change set would alter existing stack parameters")

    def tags(rows):
        return sorted((p["Key"], p["Value"]) for p in rows)

    if tags(existing.get("Tags", [])) != tags(details.get("Tags", [])):
        raise ValueError("Change set would alter existing stack tags")


def main():
    import boto3

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--environment", choices=("dev", "prod"), required=True)
    p.add_argument("--phase", choices=("services", "network"), required=True)
    p.add_argument("--mode", choices=("inspect", "plan", "execute"), default="inspect")
    p.add_argument("--receipt", required=True)
    p.add_argument("--profile")
    args = p.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name=REGION)
    if session.client("sts").get_caller_identity()["Account"] != ACCOUNT:
        raise ValueError("Wrong AWS account")
    cf = session.client("cloudformation")
    if args.phase == "network":
        parked(session.client("ecs"), session.client("application-autoscaling"), args.environment)
    suffixes = ["network"] if args.phase == "network" else ["compute", "gameserver", "sprite-worker", "worldgm"]
    stacks = [f"goosetown-{args.environment}-{suffix}" for suffix in suffixes]
    reviewed = json.loads(os.environ.get("REVIEWED_CHANGESETS", "{}"))
    if args.mode == "execute" and set(reviewed) != set(stacks):
        raise ValueError("Provide exactly the reviewed stack-to-change-set map")
    receipts = []
    for stack in stacks:
        current = deployed(cf, stack)
        existing = cf.describe_stacks(StackName=stack)["Stacks"][0]
        if any(p.get("NoEcho") for p in current.get("Parameters", {}).values()):
            raise ValueError("Opaque parameters require separate review")
        proposed, changes = transform(stack, current)
        receipt = {"stack": stack, "beforeSha256": digest(current), "afterSha256": digest(proposed), "changes": changes}
        receipts.append(receipt)
        if args.mode == "inspect":
            continue
        if not changes:
            receipt["changeSetId"] = None
            if args.mode == "execute" and reviewed[stack] is not None:
                previous = cf.describe_change_set(StackName=stack, ChangeSetName=reviewed[stack])
                if (
                    previous["StackName"] != stack
                    or previous["ExecutionStatus"] != "EXECUTE_COMPLETE"
                    or digest(deployed(cf, stack, reviewed[stack])) != digest(current)
                ):
                    raise ValueError("Current template is not the previously reviewed completed transition")
                receipt["alreadyExecuted"] = True
            continue
        if args.mode == "plan":
            name = "goose-park-" + os.environ["GITHUB_RUN_ID"] + "-" + os.environ.get("GITHUB_RUN_ATTEMPT", "1")
            parameters = [
                {"ParameterKey": p["ParameterKey"], "UsePreviousValue": True} for p in existing.get("Parameters", [])
            ]
            response = cf.create_change_set(
                StackName=stack,
                ChangeSetName=name,
                ChangeSetType="UPDATE",
                TemplateBody=encoded(proposed),
                Parameters=parameters,
                Tags=existing.get("Tags", []),
                Capabilities=["CAPABILITY_IAM", "CAPABILITY_NAMED_IAM"],
            )
            change_set = response["Id"]
            cf.get_waiter("change_set_create_complete").wait(
                StackName=stack, ChangeSetName=change_set, WaiterConfig={"Delay": 5, "MaxAttempts": 60}
            )
        else:
            change_set = reviewed[stack]
            if not isinstance(change_set, str) or not change_set.startswith(
                f"arn:aws:cloudformation:{REGION}:{ACCOUNT}:changeSet/goose-park-"
            ):
                raise ValueError("Not a parking change set")
        details = cf.describe_change_set(StackName=stack, ChangeSetName=change_set)
        if (
            details["StackName"] != stack
            or details["Status"] != "CREATE_COMPLETE"
            or details["ExecutionStatus"] != "AVAILABLE"
        ):
            raise ValueError("Change set is not executable for this stack")
        unchanged_parameters(current, existing, details)
        if digest(deployed(cf, stack, change_set)) != digest(proposed):
            raise ValueError("Reviewed change set does not equal the parking-only transition")
        receipt["changeSetId"] = change_set
        if args.mode == "execute":
            cf.execute_change_set(StackName=stack, ChangeSetName=change_set)
            cf.get_waiter("stack_update_complete").wait(StackName=stack, WaiterConfig={"Delay": 15, "MaxAttempts": 80})
            if digest(deployed(cf, stack)) != digest(proposed):
                raise ValueError("Deployed template does not match reviewed parking transition")
            receipt["executed"] = True
    Path(args.receipt).write_text(
        json.dumps(
            {"environment": args.environment, "phase": args.phase, "mode": args.mode, "stacks": receipts}, indent=2
        )
    )
    print(json.dumps(receipts, indent=2))  # hashes and allowlisted deltas only; no template/env/secret values


if __name__ == "__main__":
    main()
