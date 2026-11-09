/**
 * gameserver-stack.test.ts
 *
 * Asserts that the GameServerStack synthesizes a CloudWatch alarm that
 * detects when the gameserver service has zero running tasks for 5
 * consecutive minutes. Container Insights is disabled cluster-wide (cost),
 * so we cannot use ECS/ContainerInsights:RunningTaskCount; instead we alarm
 * on AWS/ECS:CPUUtilization SampleCount — present (>=1) when 1+ tasks run,
 * absent when zero tasks (TreatMissingData=BREACHING fires the alarm). This
 * alarm is critical because agents drop permanently if the gameserver task
 * restarts for > ~1 min (daemon reconnect caps at 5 attempts).
 */

import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as sns from "aws-cdk-lib/aws-sns";
import { GameServerStack } from "../lib/stacks/gameserver-stack";

function buildTestApp(paused=false): { template: Template } {
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
    ...{paused},
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

  test("alarm exists with MetricName CPUUtilization (proxy for task presence)", () => {
    // CPUUtilization is published per-service per-minute by Fargate when 1+
    // tasks are running. We don't care about the actual CPU value — only
    // whether the metric is being published at all (presence == tasks alive).
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      MetricName: "CPUUtilization",
    });
  });

  test("alarm pulls from AWS/ECS namespace (Container Insights is disabled)", () => {
    // Container Insights is OFF on the shared cluster (cost reasons — see
    // ClusterStack). Any alarm on ECS/ContainerInsights:RunningTaskCount
    // would latch BREACHING because the metric is never published. Use the
    // base AWS/ECS namespace, which is always populated for active services.
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      Namespace: "AWS/ECS",
    });
  });

  test("alarm uses SampleCount statistic (presence/absence detection)", () => {
    // SampleCount=1 when ECS publishes a CPU datapoint; missing when zero
    // tasks run. Average/Min/Max would also work, but SampleCount makes the
    // intent (counting datapoints, not measuring CPU) explicit.
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      Statistic: "SampleCount",
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


test("paused game server cannot be restarted by deployment",()=>{
  const {template}=buildTestApp(true);
  template.hasResourceProperties("AWS::ECS::Service",{DesiredCount:0});
  template.resourceCountIs("AWS::CloudWatch::Alarm",0);
});
