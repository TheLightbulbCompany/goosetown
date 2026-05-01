/**
 * gameserver-stack.test.ts
 *
 * Asserts that the GameServerStack synthesizes a CloudWatch alarm that
 * monitors RunningTaskCount and fires after 5 consecutive minutes below
 * threshold. This alarm is critical because agents drop permanently if the
 * game server task restarts for > ~1 min (daemon reconnect caps at 5 attempts).
 */

import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as sns from "aws-cdk-lib/aws-sns";
import { GameServerStack } from "../lib/stacks/gameserver-stack";

function buildTestApp(): { template: Template } {
  const app = new cdk.App();

  const depsStack = new cdk.Stack(app, "deps", {
    env: { account: "123456789012", region: "us-east-1" },
  });

  const vpc = ec2.Vpc.fromVpcAttributes(depsStack, "Vpc", {
    vpcId: "vpc-12345",
    availabilityZones: ["us-east-1a", "us-east-1b"],
    privateSubnetIds: ["subnet-private-1a", "subnet-private-1b"],
    publicSubnetIds: ["subnet-public-1a", "subnet-public-1b"],
  });

  // Fixture cluster — mirrors ClusterStack output (CI disabled, shared cluster).
  const fixtureCluster = new ecs.Cluster(depsStack, "FixtureCluster", {
    vpc,
    clusterName: "goosetown-dev",
    containerInsightsV2: ecs.ContainerInsights.DISABLED,
  });

  const alarmTopic = new sns.Topic(depsStack, "AlarmTopic", {
    topicName: "goosetown-dev-alarms",
  });

  const stack = new GameServerStack(app, "goosetown-dev-gameserver", {
    stackName: "goosetown-dev-gameserver",
    environment: "dev",
    env: { account: "123456789012", region: "us-east-1" },
    vpc,
    cluster: fixtureCluster,
    alarmTopic,
  });

  return { template: Template.fromStack(stack) };
}

describe("GameServerStack — CloudWatch restart alarm", () => {
  let template: Template;

  beforeAll(() => {
    ({ template } = buildTestApp());
  });

  test("alarm exists with MetricName RunningTaskCount", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      MetricName: "RunningTaskCount",
    });
  });

  test("alarm pulls from ECS/ContainerInsights namespace (not AWS/ECS)", () => {
    // RunningTaskCount is published under ECS/ContainerInsights only; using
    // the default AWS/ECS namespace produces an alarm that never sees data
    // and latches on ALARM when TreatMissingData=BREACHING.
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      Namespace: "ECS/ContainerInsights",
    });
  });

  test("GameServerStack owns zero ECS clusters (cluster lives in ClusterStack)", () => {
    // After the ClusterStack refactor, GameServerStack no longer creates its
    // own cluster — it accepts one as a constructor prop. The cluster resource
    // must live only in ClusterStack's template, not here.
    template.resourceCountIs("AWS::ECS::Cluster", 0);
  });

  test("alarm uses LessThanThreshold comparison operator", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      ComparisonOperator: "LessThanThreshold",
    });
  });

  test("alarm fires after 5 consecutive evaluation periods (5 minutes)", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      EvaluationPeriods: 5,
    });
  });

  test("alarm threshold is 1 (single task must be running)", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      Threshold: 1,
    });
  });

  test("alarm treats missing data as breaching", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      TreatMissingData: "breaching",
    });
  });

  test("alarm has an SNS action (alarm topic wired)", () => {
    // AlarmActions should contain at least one entry (the SNS topic ARN).
    // CDK renders the ARN as a Ref-resolved intrinsic, so assert that the
    // property exists and is a non-empty array without nesting anyValue()
    // inside arrayWith() (which CDK's assertion library forbids).
    const alarms = template.findResources("AWS::CloudWatch::Alarm");
    const alarmIds = Object.keys(alarms);
    expect(alarmIds.length).toBeGreaterThan(0);
    for (const id of alarmIds) {
      const actions = alarms[id].Properties?.AlarmActions;
      expect(Array.isArray(actions)).toBe(true);
      expect(actions.length).toBeGreaterThan(0);
    }
  });

  test("exactly one CloudWatch alarm is created", () => {
    template.resourceCountIs("AWS::CloudWatch::Alarm", 1);
  });
});

describe("GameServerStack — Fargate service config", () => {
  let template: Template;

  beforeAll(() => {
    ({ template } = buildTestApp());
  });

  test("game server desiredCount remains 1 (sharding deferred)", () => {
    template.hasResourceProperties("AWS::ECS::Service", {
      DesiredCount: 1,
    });
  });
});
