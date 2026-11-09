"""Strict temporary edge retirement; no images, IAM, data or certificates change."""

import copy
import json

from botocore.exceptions import ClientError
from park_legacy_world import (
    deployed,
    digest,
    execution_role,
    parked,
    unchanged_parameters,
)

ROOT_ZONE = "Z09913772LJJ6H4X9JJX0"
OLD_ZONE = "Z09248243AOUC775CDUI4"
LEGACY_DNS = "ApiDnsRecordD7F299CB"
SERVICE = "ServiceD69D759B"
EDGE = {
    "ALBAEE750D2": "AWS::ElasticLoadBalancingV2::LoadBalancer",
    "ALBHttpRedirect2FAF0637": "AWS::ElasticLoadBalancingV2::Listener",
    "ALBHttpsListenerF678A746": "AWS::ElasticLoadBalancingV2::Listener",
    "TargetGroup3D7CD9B8": "AWS::ElasticLoadBalancingV2::TargetGroup",
    "AlbWebAcl": "AWS::WAFv2::WebACL",
    "AlbWebAclAssociation": "AWS::WAFv2::WebACLAssociation",
    "ALBSecurityGroup8B8624F8": "AWS::EC2::SecurityGroup",
    LEGACY_DNS: "AWS::Route53::RecordSet",
}
INGRESS = {
    "dev": "ServiceSecurityGroupfromdevgoosetowndevcomputeALBSecurityGroup9983A7318000F9842149",
    "prod": "ServiceSecurityGroupfromprodgoosetownprodcomputeALBSecurityGroup0B42EAAF8000FAF27083",
}
EGRESS = {
    "dev": "ALBSecurityGrouptodevgoosetowndevcomputeServiceSecurityGroupB7A01C538000E7CFDFCB",
    "prod": "ALBSecurityGrouptoprodgoosetownprodcomputeServiceSecurityGroupC945CF438000BDE68EDF",
}


class AlreadyRetired(ValueError):
    """Recognizable no-action result when a prior execution receipt is needed."""

    def __init__(self, receipt):
        super().__init__("Edge already absent from template; no action; provide prior reviewed execution receipt")
        self.receipt = receipt


def host(environment):
    if environment not in INGRESS:
        raise ValueError("Unknown edge environment")
    return f"api{'-dev' if environment == 'dev' else ''}.goosetown.thelightbulb.company."


def legacy_record(template, environment):
    record = template["Resources"].get(LEGACY_DNS, {})
    props = record.get("Properties", {})
    if (
        record.get("Type") != EDGE[LEGACY_DNS]
        or props.get("Name") != f"api-{environment}.goosetown.isol8.co."
        or props.get("Type") != "A"
        or props.get("HostedZoneId") != OLD_ZONE
    ):
        raise ValueError("Unexpected legacy DNS identity")
    return record


def transform_edge(phase, stack, template):
    environment = next((env for env in INGRESS if stack == f"goosetown-{env}-compute"), None)
    if not environment or phase not in {"edge-prepare", "edge"}:
        raise ValueError("Outside edge transition scope")
    result = copy.deepcopy(template)
    resources = result["Resources"]
    service = resources[SERVICE]["Properties"]
    if service.get("DesiredCount") != 0:
        raise ValueError("Pin service to zero before retiring edge")
    if phase == "edge" and not any(key in resources for key in EDGE):
        if service.get("LoadBalancers") != [] or service.get("HealthCheckGracePeriodSeconds") != 0:
            raise ValueError("Incomplete prior edge transition")
        return result, []
    record = legacy_record(result, environment)
    if phase == "edge-prepare":
        if record.get("DeletionPolicy") == "Retain":
            return result, []
        if record.get("DeletionPolicy") not in (None, "Delete"):
            raise ValueError("Unexpected legacy DNS retention")
        record["DeletionPolicy"] = "Retain"
        return result, [{"id": LEGACY_DNS, "action": "Modify", "field": "DeletionPolicy", "value": "Retain"}]
    if record.get("DeletionPolicy") != "Retain":
        raise ValueError("Reviewed prepare step must finish before edge retirement")
    if service.get("LoadBalancers") != [
        {"ContainerName": "backend", "ContainerPort": 8000, "TargetGroupArn": {"Ref": "TargetGroup3D7CD9B8"}}
    ]:
        raise ValueError("Unexpected service load-balancer attachment")
    removals = {
        **EDGE,
        INGRESS[environment]: "AWS::EC2::SecurityGroupIngress",
        EGRESS[environment]: "AWS::EC2::SecurityGroupEgress",
    }
    for logical, kind in removals.items():
        if resources.get(logical, {}).get("Type") != kind:
            raise ValueError("Unexpected edge resource identity")
        if logical != LEGACY_DNS and resources[logical].get("DeletionPolicy") not in (None, "Delete"):
            raise ValueError("Edge resource would be retained rather than retired")
    for logical, source, destination, destination_field in (
        (INGRESS[environment], "ServiceSecurityGroupC96ED6A7", "ALBSecurityGroup8B8624F8", "SourceSecurityGroupId"),
        (EGRESS[environment], "ALBSecurityGroup8B8624F8", "ServiceSecurityGroupC96ED6A7", "DestinationSecurityGroupId"),
    ):
        rule = resources[logical].get("Properties", {})
        if (
            rule.get("GroupId") != {"Fn::GetAtt": [source, "GroupId"]}
            or rule.get(destination_field) != {"Fn::GetAtt": [destination, "GroupId"]}
            or rule.get("FromPort") != 8000
            or rule.get("ToPort") != 8000
            or rule.get("IpProtocol") != "tcp"
        ):
            raise ValueError("Unexpected ALB-only security group rule")
    for logical in removals:
        del resources[logical]
    service["LoadBalancers"] = []
    service["HealthCheckGracePeriodSeconds"] = 0
    for logical, resource in resources.items():
        if "DependsOn" in resource:
            dependencies = resource["DependsOn"]
            dependencies = [dependencies] if isinstance(dependencies, str) else dependencies
            kept = [key for key in dependencies if key not in removals]
            if kept != dependencies:
                if logical != SERVICE or set(dependencies) - set(kept) != {"ALBHttpsListenerF678A746"}:
                    raise ValueError("Unreviewed resource depends on retired edge")
                if kept:
                    resource["DependsOn"] = kept
                else:
                    del resource["DependsOn"]
    output = result.get("Outputs", {}).get("AlbDnsName")
    if not output or "Export" in output:
        raise ValueError("Unexpected or exported ALB output")
    del result["Outputs"]["AlbDnsName"]

    # Any remaining reference is outside the reviewed transition; never guess a replacement.
    def check(node):
        if isinstance(node, dict):
            if node.get("Ref") in removals:
                raise ValueError("Dangling edge reference")
            att = node.get("Fn::GetAtt")
            if att and (att[0] if isinstance(att, list) else att.split(".")[0]) in removals:
                raise ValueError("Dangling edge reference")
            if "Fn::Sub" in node and any(
                f"${{{key}}}" in json.dumps(node["Fn::Sub"]) or f"${{{key}." in json.dumps(node["Fn::Sub"])
                for key in removals
            ):
                raise ValueError("Dangling edge reference")
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(result)
    changes = [{"id": key, "action": "Remove", "type": kind} for key, kind in removals.items()]
    changes += [
        {"id": SERVICE, "action": "Modify", "fields": {"LoadBalancers": [], "HealthCheckGracePeriodSeconds": 0}},
        {"id": "AlbDnsName", "action": "RemoveOutput"},
    ]
    return result, changes


def records(client, zone):
    return [
        r
        for page in client.get_paginator("list_resource_record_sets").paginate(HostedZoneId=zone)
        for r in page["ResourceRecordSets"]
    ]


def terminal_proof(session, environment, template, reviewed_change_set):
    resources = template["Resources"]
    service = resources.get(SERVICE, {}).get("Properties", {})
    if (
        any(key in resources for key in [*EDGE, INGRESS[environment], EGRESS[environment]])
        or service.get("DesiredCount") != 0
        or service.get("LoadBalancers") != []
        or service.get("HealthCheckGracePeriodSeconds") != 0
    ):
        raise ValueError("Incomplete terminal edge template")
    route53 = session.client("route53")
    if any(r["Name"] == host(environment) for r in records(route53, ROOT_ZONE)) or any(
        r["Name"] == f"api-{environment}.goosetown.isol8.co." for r in records(route53, OLD_ZONE)
    ):
        raise ValueError("Terminal edge still has DNS")
    receipt = {
        "alreadyRetired": True,
        "allServicesAndTasksParked": True,
        "canonicalAndLegacyDnsAbsent": True,
        "physicalAbsenceVerified": False,
    }
    if not reviewed_change_set:
        raise AlreadyRetired({**receipt, "status": "already_retired_no_action_requires_prior_receipt"})
    cf = session.client("cloudformation")
    stack = f"goosetown-{environment}-compute"
    existing = cf.describe_stacks(StackName=stack)["Stacks"][0]
    execution_role(existing)
    details = cf.describe_change_set(StackName=stack, ChangeSetName=reviewed_change_set)
    if (
        details["StackName"] != stack
        or details["ExecutionStatus"] != "EXECUTE_COMPLETE"
        or digest(deployed(cf, stack, reviewed_change_set)) != digest(template)
    ):
        raise ValueError("Terminal template is not the exact reviewed completed transition")
    unchanged_parameters(template, existing, details)
    removed = {
        c["ResourceChange"]["LogicalResourceId"]: c["ResourceChange"].get("PhysicalResourceId")
        for c in details["Changes"]
        if c["ResourceChange"]["Action"] == "Remove"
    }

    def absent(client, method, arguments, codes):
        try:
            getattr(client, method)(**arguments)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in codes:
                return
            raise
        raise ValueError("Retired edge resource still physically exists")

    for key in ("ALBAEE750D2", "TargetGroup3D7CD9B8", "ALBSecurityGroup8B8624F8", "AlbWebAcl"):
        if not removed.get(key):
            raise AlreadyRetired({**receipt, "status": "already_retired_no_action_missing_physical_receipt"})
    elb = session.client("elbv2")
    absent(elb, "describe_load_balancers", {"LoadBalancerArns": [removed["ALBAEE750D2"]]}, {"LoadBalancerNotFound"})
    absent(
        elb, "describe_target_groups", {"TargetGroupArns": [removed["TargetGroup3D7CD9B8"]]}, {"TargetGroupNotFound"}
    )
    absent(
        session.client("ec2"),
        "describe_security_groups",
        {"GroupIds": [removed["ALBSecurityGroup8B8624F8"]]},
        {"InvalidGroup.NotFound"},
    )
    waf_identity = removed["AlbWebAcl"].split("|")
    if len(waf_identity) != 3 or waf_identity[0] != f"goosetown-{environment}-alb-waf" or waf_identity[2] != "REGIONAL":
        raise ValueError("Unexpected retired WAF receipt identity")
    absent(
        session.client("wafv2"),
        "get_web_acl",
        {"Name": waf_identity[0], "Id": waf_identity[1], "Scope": "REGIONAL"},
        {"WAFNonexistentItemException"},
    )
    return {**receipt, "physicalAbsenceVerified": True, "completedChangeSetId": reviewed_change_set}


def preflight(session, environment, phase, template, reviewed_change_set=None):
    """No probes/invokes: only current control-plane metadata and zero-task guards."""
    parked(session.client("ecs"), session.client("application-autoscaling"), environment)
    cf = session.client("cloudformation")
    resources = {
        r["LogicalResourceId"]: r
        for page in cf.get_paginator("list_stack_resources").paginate(StackName=f"goosetown-{environment}-compute")
        for r in page["StackResourceSummaries"]
    }
    if phase == "edge" and "ALBAEE750D2" not in template["Resources"]:
        return terminal_proof(session, environment, template, reviewed_change_set)
    alb_arn = resources["ALBAEE750D2"]["PhysicalResourceId"]
    elb = session.client("elbv2")
    alb = elb.describe_load_balancers(LoadBalancerArns=[alb_arn])["LoadBalancers"][0]
    if alb.get("LoadBalancerName") != f"goosetown-{environment}":
        raise ValueError("Unexpected ALB identity")
    groups = elb.describe_target_groups(LoadBalancerArn=alb_arn)["TargetGroups"]
    if (
        len(groups) != 1
        or groups[0]["TargetGroupName"] != f"goosetown-{environment}-tg"
        or groups[0]["LoadBalancerArns"] != [alb_arn]
    ):
        raise ValueError("Target group has unexpected consumers")
    if elb.describe_target_health(TargetGroupArn=groups[0]["TargetGroupArn"])["TargetHealthDescriptions"]:
        raise ValueError("Target group still has live targets")
    listeners = elb.describe_listeners(LoadBalancerArn=alb_arn)["Listeners"]
    if len(listeners) != 2 or {listener["Port"] for listener in listeners} != {80, 443}:
        raise ValueError("ALB has unexpected listeners")
    for listener in listeners:
        actions = listener["DefaultActions"]
        if len(actions) != 1 or actions[0]["Type"] != ("redirect" if listener["Port"] == 80 else "forward"):
            raise ValueError("ALB has unexpected listener actions")
        if listener["Port"] == 443 and actions[0].get("TargetGroupArn") != groups[0]["TargetGroupArn"]:
            raise ValueError("Listener forwards outside its reviewed target group")
    waf = session.client("wafv2")
    acl = waf.get_web_acl_for_resource(ResourceArn=alb_arn).get("WebACL", {})
    if acl.get("Name") != f"goosetown-{environment}-alb-waf" or waf.list_resources_for_web_acl(
        WebACLArn=acl["ARN"], ResourceType="APPLICATION_LOAD_BALANCER"
    )["ResourceArns"] != [alb_arn]:
        raise ValueError("WAF has unexpected associations")
    endpoints = [alb_arn, alb["DNSName"], groups[0]["TargetGroupArn"], acl["ARN"]]
    for page in cf.get_paginator("list_exports").paginate():
        if any(any(value in str(export.get("Value", "")) for value in endpoints) for export in page["Exports"]):
            raise ValueError("Edge endpoint is exported to potential import consumers")
    sg = resources["ALBSecurityGroup8B8624F8"]["PhysicalResourceId"]
    interfaces = [
        interface
        for page in session.client("ec2")
        .get_paginator("describe_network_interfaces")
        .paginate(Filters=[{"Name": "group-id", "Values": [sg]}])
        for interface in page["NetworkInterfaces"]
    ]
    if any(
        not interface.get("RequesterManaged")
        or not interface.get("Description", "").startswith(f"ELB app/goosetown-{environment}/")
        for interface in interfaces
    ):
        raise ValueError("ALB security group has another consumer")
    route53 = session.client("route53")
    name = host(environment)
    aliases = []
    for page in route53.get_paginator("list_hosted_zones").paginate():
        for zone in page["HostedZones"]:
            for record in records(route53, zone["Id"]):
                dns = record.get("AliasTarget", {}).get("DNSName", "").lower().rstrip(".").removeprefix("dualstack.")
                cname_targets = (
                    [
                        r.get("Value", "").lower().rstrip(".").removeprefix("dualstack.")
                        for r in record.get("ResourceRecords", [])
                    ]
                    if record.get("Type") == "CNAME"
                    else []
                )
                endpoints = {alb["DNSName"].lower().rstrip("."), name.lower().rstrip(".")}
                if dns in endpoints or any(target in endpoints for target in cname_targets):
                    aliases.append((zone["Id"].split("/")[-1], record))
    current = [r for r in records(route53, ROOT_ZONE) if r["Name"] == name]
    if len(current) > 1 or any(r.get("Type") != "A" or set(r) != {"Name", "Type", "AliasTarget"} for r in current):
        raise ValueError("Unexpected canonical record shape")
    if aliases != ([(ROOT_ZONE, current[0])] if current else []):
        raise ValueError("ALB has unexpected DNS aliases")
    if any(r["Name"] == f"api-{environment}.goosetown.isol8.co." for r in records(route53, OLD_ZONE)):
        raise ValueError("Legacy DNS record is not actually absent")
    if phase in {"dns", "edge"} and legacy_record(template, environment).get("DeletionPolicy") != "Retain":
        raise ValueError("Prepare metadata must be applied first")
    if phase == "edge" and current:
        raise ValueError("Canonical DNS deletion must be reviewed/applied first")
    return {
        "albArn": alb_arn,
        "dnsRecord": current[0] if current else None,
        "liveTargets": 0,
        "wafAssociationCount": 1,
        "allServicesAndTasksParked": True,
    }


def dns_operation(session, environment, mode, reviewed_hash, proof):
    record = proof["dnsRecord"]
    if record and (
        record.get("Name") != host(environment)
        or record.get("Type") != "A"
        or set(record) != {"Name", "Type", "AliasTarget"}
    ):
        raise ValueError("DNS operation is outside its exact canonical record scope")
    receipt = {
        "environment": environment,
        "phase": "dns",
        "mode": mode,
        "zone": ROOT_ZONE,
        "record": proof["dnsRecord"],
        "preflight": proof,
    }
    receipt["recordSha256"] = digest({"zone": ROOT_ZONE, "record": proof["dnsRecord"]})
    if mode == "execute":
        if not proof["dnsRecord"] or reviewed_hash != receipt["recordSha256"]:
            raise ValueError("DNS differs from exact reviewed hash or is already absent")
        response = session.client("route53").change_resource_record_sets(
            HostedZoneId=ROOT_ZONE,
            ChangeBatch={"Changes": [{"Action": "DELETE", "ResourceRecordSet": proof["dnsRecord"]}]},
        )
        session.client("route53").get_waiter("resource_record_sets_changed").wait(Id=response["ChangeInfo"]["Id"])
        if any(r["Name"] == host(environment) for r in records(session.client("route53"), ROOT_ZONE)):
            raise ValueError("DNS deletion did not complete")
        receipt["deleted"] = True
    return receipt
