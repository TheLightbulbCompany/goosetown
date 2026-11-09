import copy
import unittest
from unittest.mock import Mock

from park_legacy_world import EXECUTION_ROLE, execution_role, parked, transform, unchanged_parameters


class ParkingTests(unittest.TestCase):
    def test_draining_standalone_task_blocks_nat_retirement(self):
        ecs, scaling = Mock(), Mock()
        services = [f"service-{i}" for i in range(4)]
        ecs.get_paginator.return_value.paginate.return_value = [{"serviceArns": services}]
        ecs.describe_services.return_value = {
            "services": [{"serviceName": s, "desiredCount": 0, "runningCount": 0, "pendingCount": 0} for s in services]
        }
        ecs.list_tasks.side_effect = lambda **kw: {"taskArns": ["draining"] if kw["desiredStatus"] == "STOPPED" else []}
        ecs.describe_tasks.return_value = {"tasks": [{"taskArn": "draining", "lastStatus": "RUNNING"}]}
        scaling.describe_scalable_targets.return_value = {"ScalableTargets": []}
        with self.assertRaisesRegex(ValueError, "draining"):
            parked(ecs, scaling, "dev")
        ecs.describe_tasks.return_value = {"tasks": [{"taskArn": "draining", "lastStatus": "STOPPED"}]}
        parked(ecs, scaling, "dev")
        ecs.list_tasks.side_effect = [
            {"taskArns": []},
            {"taskArns": []},
            {"taskArns": ["done"], "nextToken": "second"},
            {"taskArns": ["draining"]},
        ]
        ecs.describe_tasks.side_effect = [
            {"tasks": [{"lastStatus": "STOPPED"}]},
            {"tasks": [{"lastStatus": "DEPROVISIONING"}]},
        ]
        with self.assertRaisesRegex(ValueError, "draining"):
            parked(ecs, scaling, "dev")

    def test_requires_existing_cdk_execution_role_without_broadening_caller_permissions(self):
        execution_role({"RoleARN": EXECUTION_ROLE})
        for stack in ({}, {"RoleARN": "arn:aws:iam::877352799272:role/other"}):
            with self.assertRaisesRegex(ValueError, "execution role"):
                execution_role(stack)

    def test_services_only_changes_count_and_removes_known_autoscaling(self):
        template = {
            "Resources": {
                "ServiceD69D759B": {
                    "Type": "AWS::ECS::Service",
                    "Properties": {
                        "DesiredCount": 1,
                        "TaskDefinition": {"Ref": "Task"},
                        "DeploymentConfiguration": {"MaximumPercent": 200},
                    },
                },
                "Task": {
                    "Type": "AWS::ECS::TaskDefinition",
                    "Properties": {
                        "ContainerDefinitions": [
                            {"Image": "existing-image", "Environment": [{"Name": "EXISTING", "Value": "unchanged"}]}
                        ]
                    },
                },
                "Role": {"Type": "AWS::IAM::Role", "Properties": {"Policies": [{"unchanged": True}]}},
                "Dependent": {"Type": "AWS::S3::Bucket", "DependsOn": "Role"},
                "Dns": {"Type": "AWS::Route53::RecordSet", "Properties": {"Name": "existing.example"}},
                "Data": {"Type": "AWS::DynamoDB::Table", "DeletionPolicy": "Retain"},
                "ServiceTaskCountTarget23E25614": {"Type": "AWS::ApplicationAutoScaling::ScalableTarget"},
                "ServiceTaskCountTargetCpuScalingCC8A5DF6": {"Type": "AWS::ApplicationAutoScaling::ScalingPolicy"},
            },
            "Outputs": {"Preserved": {"Value": "unchanged"}},
        }
        original = copy.deepcopy(template)
        changed, delta = transform("goosetown-prod-compute", template)
        expected = copy.deepcopy(original)
        expected["Resources"]["ServiceD69D759B"]["Properties"]["DesiredCount"] = 0
        del (
            expected["Resources"]["ServiceTaskCountTarget23E25614"],
            expected["Resources"]["ServiceTaskCountTargetCpuScalingCC8A5DF6"],
        )
        self.assertEqual(changed, expected)
        self.assertEqual(template, original)
        self.assertEqual(len(delta), 3)
        self.assertEqual(transform("goosetown-prod-compute", changed), (changed, []))

    def test_network_retirement_preserves_vpc_subnets_data_routes_and_outputs(self):
        template = {
            "Resources": {
                "VpcpublicSubnet1NATGatewayA036E8A6": {"Type": "AWS::EC2::NatGateway"},
                "VpcpublicSubnet1EIP411541E6": {"Type": "AWS::EC2::EIP"},
                "VpcprivateSubnet1DefaultRouteB506891A": {"Type": "AWS::EC2::Route"},
                "VpcprivateSubnet2DefaultRouteBAC3C1C3": {"Type": "AWS::EC2::Route"},
                "NatPortErrorAlarmCC295C98": {"Type": "AWS::CloudWatch::Alarm"},
                "Vpc": {"Type": "AWS::EC2::VPC"},
                "Subnet": {"Type": "AWS::EC2::Subnet"},
                "Endpoint": {"Type": "AWS::EC2::VPCEndpoint"},
                "PublicRoute": {"Type": "AWS::EC2::Route"},
            },
            "Outputs": {"Vpc": {"Value": {"Ref": "Vpc"}}},
        }
        changed, delta = transform("goosetown-dev-network", template)
        self.assertEqual(set(changed["Resources"]), {"Vpc", "Subnet", "Endpoint", "PublicRoute"})
        self.assertEqual(changed["Outputs"], template["Outputs"])
        self.assertEqual(len(delta), 5)

    def test_output_reference_prevents_unsafe_retirement(self):
        template = {
            "Resources": {"VpcpublicSubnet1EIP411541E6": {"Type": "AWS::EC2::EIP"}},
            "Outputs": {"Used": {"Value": {"Ref": "VpcpublicSubnet1EIP411541E6"}}},
        }
        with self.assertRaisesRegex(ValueError, "dangling"):
            transform("goosetown-prod-network", template)

    def test_unexpected_logical_identity_is_never_modified(self):
        for stack, template in [
            ("lightbulb-prod-network", {"Resources": {}}),
            ("goosetown-prod-compute", {"Resources": {"Other": {"Type": "AWS::ECS::Service"}}}),
            ("goosetown-prod-network", {"Resources": {"VpcpublicSubnet1EIP411541E6": {"Type": "AWS::S3::Bucket"}}}),
        ]:
            with self.assertRaises(ValueError):
                transform(stack, template)

    def test_stateful_resource_with_retired_dependency_is_rejected(self):
        template = {
            "Resources": {
                "VpcpublicSubnet1NATGatewayA036E8A6": {"Type": "AWS::EC2::NatGateway"},
                "Data": {
                    "Type": "AWS::S3::Bucket",
                    "Properties": {"Unexpected": {"Fn::GetAtt": ["VpcpublicSubnet1NATGatewayA036E8A6", "Id"]}},
                },
            }
        }
        with self.assertRaisesRegex(ValueError, "dangling"):
            transform("goosetown-dev-network", template)

    def test_same_template_cannot_smuggle_parameter_or_tag_change(self):
        old = {
            "Parameters": [{"ParameterKey": "image", "ParameterValue": "kept"}],
            "Tags": [{"Key": "Project", "Value": "goosetown"}],
        }
        unchanged_parameters({}, old, old)
        changed = copy.deepcopy(old)
        changed["Parameters"][0]["ParameterValue"] = "different"
        with self.assertRaisesRegex(ValueError, "parameters"):
            unchanged_parameters({}, old, changed)
        changed = copy.deepcopy(old)
        changed["Tags"][0]["Value"] = "different"
        with self.assertRaisesRegex(ValueError, "tags"):
            unchanged_parameters({}, old, changed)
        with self.assertRaisesRegex(ValueError, "Opaque"):
            unchanged_parameters({"Parameters": {"password": {"NoEcho": True}}}, old, old)


if __name__ == "__main__":
    unittest.main()
