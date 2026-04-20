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

  const alarmTopic = new sns.Topic(depsStack, "AlarmTopic", {
    topicName: "goosetown-dev-alarms",
  });

  const stack = new GameServerStack(app, "goosetown-dev-gameserver", {
    stackName: "goosetown-dev-gameserver",
    environment: "dev",
    env: { account: "123456789012", region: "us-east-1" },
    vpc,
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
    // AlarmActions should contain at least one entry (the SNS topic ARN)
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmActions: Match.arrayWith([Match.anyValue()]),
    });
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
