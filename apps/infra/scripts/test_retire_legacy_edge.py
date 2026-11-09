import copy
import unittest
from unittest.mock import Mock, patch

import retire_legacy_edge as edge
from botocore.exceptions import ClientError
from park_legacy_world import EXECUTION_ROLE


def template(env="dev"):
    resources = {name: {"Type": kind} for name, kind in edge.EDGE.items()}
    resources[edge.INGRESS[env]] = {"Type": "AWS::EC2::SecurityGroupIngress"}
    resources[edge.EGRESS[env]] = {"Type": "AWS::EC2::SecurityGroupEgress"}
    for logical, source, destination, field in (
        (edge.INGRESS[env], "ServiceSecurityGroupC96ED6A7", "ALBSecurityGroup8B8624F8", "SourceSecurityGroupId"),
        (edge.EGRESS[env], "ALBSecurityGroup8B8624F8", "ServiceSecurityGroupC96ED6A7", "DestinationSecurityGroupId"),
    ):
        resources[logical]["Properties"] = {
            "GroupId": {"Fn::GetAtt": [source, "GroupId"]},
            field: {"Fn::GetAtt": [destination, "GroupId"]},
            "FromPort": 8000,
            "ToPort": 8000,
            "IpProtocol": "tcp",
        }
    resources[edge.LEGACY_DNS]["Properties"] = {
        "Name": f"api-{env}.goosetown.isol8.co.",
        "HostedZoneId": edge.OLD_ZONE,
        "Type": "A",
    }
    resources[edge.SERVICE] = {
        "Type": "AWS::ECS::Service",
        "DependsOn": ["ALBHttpsListenerF678A746", "Role"],
        "Properties": {
            "DesiredCount": 0,
            "LoadBalancers": [
                {"ContainerName": "backend", "ContainerPort": 8000, "TargetGroupArn": {"Ref": "TargetGroup3D7CD9B8"}}
            ],
            "HealthCheckGracePeriodSeconds": 60,
            "TaskDefinition": {"Ref": "Task"},
        },
    }
    resources.update(
        {
            "Task": {
                "Type": "AWS::ECS::TaskDefinition",
                "Properties": {
                    "ContainerDefinitions": [
                        {"Image": "unchanged-image", "Environment": [{"Name": "CONFIG", "Value": "keep"}]}
                    ]
                },
            },
            "Role": {"Type": "AWS::IAM::Role", "Properties": {"unchanged": True}},
            "Logs": {"Type": "AWS::S3::Bucket", "DeletionPolicy": "Retain"},
            "Data": {"Type": "AWS::DynamoDB::Table", "DeletionPolicy": "Retain"},
            "Certificate": {"Type": "AWS::CertificateManager::Certificate", "Properties": {"unchanged": True}},
        }
    )
    return {
        "Resources": resources,
        "Outputs": {
            "AlbDnsName": {"Value": {"Fn::GetAtt": ["ALBAEE750D2", "DNSName"]}},
            "ServiceName": {"Value": {"Fn::GetAtt": [edge.SERVICE, "Name"]}},
        },
    }


class EdgeTests(unittest.TestCase):
    def test_prepare_changes_only_retention_and_edge_refuses_before_it(self):
        before = template()
        with self.assertRaisesRegex(ValueError, "prepare"):
            edge.transform_edge("edge", "goosetown-dev-compute", before)
        prepared, delta = edge.transform_edge("edge-prepare", "goosetown-dev-compute", before)
        expected = copy.deepcopy(before)
        expected["Resources"][edge.LEGACY_DNS]["DeletionPolicy"] = "Retain"
        self.assertEqual(prepared, expected)
        self.assertEqual(len(delta), 1)
        self.assertNotIn("DeletionPolicy", before["Resources"][edge.LEGACY_DNS])
        self.assertEqual(edge.transform_edge("edge-prepare", "goosetown-dev-compute", prepared), (prepared, []))

    def test_edge_preserves_every_nonedge_resource_and_only_detaches_stopped_service(self):
        for env in ("dev", "prod"):
            prepared, _ = edge.transform_edge("edge-prepare", f"goosetown-{env}-compute", template(env))
            result, _ = edge.transform_edge("edge", f"goosetown-{env}-compute", prepared)
            for key in ("Task", "Role", "Data", "Logs", "Certificate"):
                self.assertEqual(result["Resources"][key], prepared["Resources"][key])
            expected_service = copy.deepcopy(prepared["Resources"][edge.SERVICE])
            expected_service["Properties"].update(LoadBalancers=[], HealthCheckGracePeriodSeconds=0)
            expected_service["DependsOn"] = ["Role"]
            self.assertEqual(result["Resources"][edge.SERVICE], expected_service)
            self.assertEqual(result["Outputs"], {"ServiceName": prepared["Outputs"]["ServiceName"]})
            self.assertEqual(edge.transform_edge("edge", f"goosetown-{env}-compute", result), (result, []))

    def test_unknown_consumers_identity_or_retained_billable_resources_fail_closed(self):
        prepared, _ = edge.transform_edge("edge-prepare", "goosetown-dev-compute", template())
        for mutate in (
            lambda t: t["Resources"][edge.SERVICE]["Properties"].update(DesiredCount=1),
            lambda t: t["Resources"]["ALBAEE750D2"].update(DeletionPolicy="Retain"),
            lambda t: t["Outputs"]["AlbDnsName"].update(Export={"Name": "shared"}),
            lambda t: t["Resources"].update(
                OtherConsumer={"Type": "AWS::S3::Bucket", "Properties": {"Target": {"Ref": "ALBAEE750D2"}}}
            ),
            lambda t: t["Resources"][edge.LEGACY_DNS]["Properties"].update(Name="api.thelightbulb.company."),
            lambda t: t["Resources"]["Logs"].update(DependsOn=["ALBAEE750D2"]),
        ):
            bad = copy.deepcopy(prepared)
            mutate(bad)
            with self.assertRaises(ValueError):
                edge.transform_edge("edge", "goosetown-dev-compute", bad)

    def test_dns_exact_hash_is_required_and_only_exact_delete_is_sent(self):
        route53, session = Mock(), Mock()
        session.client.return_value = route53
        route53.get_paginator.return_value.paginate.return_value = [{"ResourceRecordSets": []}]
        route53.change_resource_record_sets.return_value = {"ChangeInfo": {"Id": "change/owned"}}
        record = {
            "Name": edge.host("dev"),
            "Type": "A",
            "AliasTarget": {"DNSName": "goose-alb.", "HostedZoneId": "ZALB", "EvaluateTargetHealth": True},
        }
        proof = {"dnsRecord": record}
        plan = edge.dns_operation(session, "dev", "plan", "", proof)
        route53.change_resource_record_sets.assert_not_called()
        with self.assertRaisesRegex(ValueError, "reviewed"):
            edge.dns_operation(session, "dev", "execute", "wrong", proof)
        result = edge.dns_operation(session, "dev", "execute", plan["recordSha256"], proof)
        self.assertTrue(result["deleted"])
        route53.change_resource_record_sets.assert_called_once_with(
            HostedZoneId=edge.ROOT_ZONE, ChangeBatch={"Changes": [{"Action": "DELETE", "ResourceRecordSet": record}]}
        )

    @patch.object(edge, "parked")
    def test_fresh_alias_exports_associations_and_sg_consumers_are_fenced(self, parked):
        cf, elb, waf, ec2, route53, session = [Mock() for _ in range(6)]
        session.client.side_effect = lambda service: {
            "cloudformation": cf,
            "elbv2": elb,
            "wafv2": waf,
            "ec2": ec2,
            "route53": route53,
            "ecs": Mock(),
            "application-autoscaling": Mock(),
        }[service]
        resources = [
            {"LogicalResourceId": "ALBAEE750D2", "PhysicalResourceId": "owned-alb"},
            {"LogicalResourceId": "ALBSecurityGroup8B8624F8", "PhysicalResourceId": "owned-sg"},
        ]
        exports = []
        cf.get_paginator.side_effect = lambda operation: Mock(
            paginate=lambda **_: [
                {"StackResourceSummaries": resources} if operation == "list_stack_resources" else {"Exports": exports}
            ]
        )
        elb.describe_load_balancers.return_value = {
            "LoadBalancers": [{"LoadBalancerName": "goosetown-dev", "DNSName": "goose-alb"}]
        }
        elb.describe_target_groups.return_value = {
            "TargetGroups": [
                {"TargetGroupName": "goosetown-dev-tg", "LoadBalancerArns": ["owned-alb"], "TargetGroupArn": "owned-tg"}
            ]
        }
        elb.describe_target_health.return_value = {"TargetHealthDescriptions": []}
        elb.describe_listeners.return_value = {
            "Listeners": [
                {"Port": 80, "DefaultActions": [{"Type": "redirect"}]},
                {"Port": 443, "DefaultActions": [{"Type": "forward", "TargetGroupArn": "owned-tg"}]},
            ]
        }
        waf.get_web_acl_for_resource.return_value = {"WebACL": {"Name": "goosetown-dev-alb-waf", "ARN": "owned-acl"}}
        waf.list_resources_for_web_acl.return_value = {"ResourceArns": ["owned-alb"]}
        ec2.describe_network_interfaces.return_value = {
            "NetworkInterfaces": [{"RequesterManaged": True, "Description": "ELB app/goosetown-dev/id"}]
        }
        ec2.get_paginator.return_value.paginate.side_effect = lambda **_: [ec2.describe_network_interfaces.return_value]
        record = {
            "Name": edge.host("dev"),
            "Type": "A",
            "AliasTarget": {"DNSName": "goose-alb.", "HostedZoneId": "ZALB"},
        }
        zones = {edge.ROOT_ZONE: [record], edge.OLD_ZONE: []}

        def paginator(operation):
            return Mock(
                paginate=lambda **kwargs: (
                    [{"HostedZones": [{"Id": z} for z in zones]}]
                    if operation == "list_hosted_zones"
                    else [{"ResourceRecordSets": zones[kwargs["HostedZoneId"]]}]
                )
            )

        route53.get_paginator.side_effect = paginator
        before = template()
        proof = edge.preflight(session, "dev", "edge-prepare", before)
        self.assertEqual(proof["dnsRecord"], record)
        prepared, _ = edge.transform_edge("edge-prepare", "goosetown-dev-compute", before)
        with self.assertRaisesRegex(ValueError, "DNS deletion"):
            edge.preflight(session, "dev", "edge", prepared)
        zones[edge.ROOT_ZONE].append({**record, "Name": "api.thelightbulb.company."})
        with self.assertRaisesRegex(ValueError, "aliases"):
            edge.preflight(session, "dev", "edge-prepare", before)
        zones[edge.ROOT_ZONE].pop()
        for target in ("goose-alb.", edge.host("dev")):
            zones[edge.ROOT_ZONE].append(
                {"Name": "dependent.example.", "Type": "CNAME", "ResourceRecords": [{"Value": target}]}
            )
            with self.assertRaisesRegex(ValueError, "aliases"):
                edge.preflight(session, "dev", "edge-prepare", before)
            zones[edge.ROOT_ZONE].pop()
        exports.append({"Value": "owned-alb"})
        with self.assertRaisesRegex(ValueError, "exported"):
            edge.preflight(session, "dev", "edge-prepare", before)
        exports.clear()
        waf.list_resources_for_web_acl.return_value = {"ResourceArns": ["owned-alb", "other-alb"]}
        with self.assertRaisesRegex(ValueError, "associations"):
            edge.preflight(session, "dev", "edge-prepare", before)
        waf.list_resources_for_web_acl.return_value = {"ResourceArns": ["owned-alb"]}
        ec2.describe_network_interfaces.return_value = {
            "NetworkInterfaces": [{"RequesterManaged": False, "Description": "other workload"}]
        }
        with self.assertRaisesRegex(ValueError, "another consumer"):
            edge.preflight(session, "dev", "edge-prepare", before)

    def test_terminal_retry_requires_completed_exact_receipt_and_physical_absence(self):
        prepared, _ = edge.transform_edge("edge-prepare", "goosetown-dev-compute", template())
        finished, _ = edge.transform_edge("edge", "goosetown-dev-compute", prepared)
        cf, elb, ec2, waf, route53, session = [Mock() for _ in range(6)]
        session.client.side_effect = lambda name: {
            "cloudformation": cf,
            "elbv2": elb,
            "ec2": ec2,
            "wafv2": waf,
            "route53": route53,
        }[name]
        route53.get_paginator.return_value.paginate.return_value = [{"ResourceRecordSets": []}]
        with self.assertRaises(edge.AlreadyRetired) as missing:
            edge.terminal_proof(session, "dev", finished, None)
        self.assertFalse(missing.exception.receipt["physicalAbsenceVerified"])
        cf.describe_stacks.return_value = {"Stacks": [{"RoleARN": EXECUTION_ROLE}]}
        physicals = {
            "ALBAEE750D2": "old-alb",
            "TargetGroup3D7CD9B8": "old-tg",
            "ALBSecurityGroup8B8624F8": "old-sg",
            "AlbWebAcl": "goosetown-dev-alb-waf|old-id|REGIONAL",
        }
        cf.describe_change_set.return_value = {
            "StackName": "goosetown-dev-compute",
            "ExecutionStatus": "EXECUTE_COMPLETE",
            "Changes": [
                {"ResourceChange": {"LogicalResourceId": name, "Action": "Remove", "PhysicalResourceId": value}}
                for name, value in physicals.items()
            ],
        }
        cf.get_template.return_value = {"TemplateBody": finished}

        def missing_resource(code):
            return ClientError({"Error": {"Code": code, "Message": "absent"}}, "Describe")

        elb.describe_load_balancers.side_effect = missing_resource("LoadBalancerNotFound")
        elb.describe_target_groups.side_effect = missing_resource("TargetGroupNotFound")
        ec2.describe_security_groups.side_effect = missing_resource("InvalidGroup.NotFound")
        waf.get_web_acl.side_effect = missing_resource("WAFNonexistentItemException")
        self.assertTrue(edge.terminal_proof(session, "dev", finished, "reviewed")["physicalAbsenceVerified"])
        waf.get_web_acl.side_effect = None
        waf.get_web_acl.return_value = {"WebACL": {"Name": "still-present"}}
        with self.assertRaisesRegex(ValueError, "physically exists"):
            edge.terminal_proof(session, "dev", finished, "reviewed")
        cf.describe_change_set.return_value["ExecutionStatus"] = "AVAILABLE"
        with self.assertRaisesRegex(ValueError, "completed"):
            edge.terminal_proof(session, "dev", finished, "reviewed")

    @patch.object(edge, "parked")
    def test_live_targets_stop_preflight_before_dns_work(self, parked):
        session = Mock()
        cf, elb = Mock(), Mock()
        session.client.side_effect = lambda service: cf if service == "cloudformation" else elb
        cf.get_paginator.return_value.paginate.return_value = [
            {"StackResourceSummaries": [{"LogicalResourceId": "ALBAEE750D2", "PhysicalResourceId": "owned-alb"}]}
        ]
        elb.describe_load_balancers.return_value = {"LoadBalancers": [{"LoadBalancerName": "goosetown-dev"}]}
        elb.describe_target_groups.return_value = {
            "TargetGroups": [
                {"TargetGroupName": "goosetown-dev-tg", "LoadBalancerArns": ["owned-alb"], "TargetGroupArn": "owned-tg"}
            ]
        }
        elb.describe_target_health.return_value = {"TargetHealthDescriptions": [{"Target": {"Id": "active"}}]}
        with self.assertRaisesRegex(ValueError, "live targets"):
            edge.preflight(session, "dev", "edge-prepare", template())
        parked.assert_called_once()


if __name__ == "__main__":
    unittest.main()
