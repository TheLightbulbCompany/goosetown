/**
 * network-stack.test.ts
 *
 * Asserts that:
 * 1. The NetworkStack creates exactly 1 NAT gateway (multi-NAT is deferred
 *    to issue #165 — this test documents the intentional single-NAT decision).
 * 2. A CloudWatch alarm exists on the NAT/ErrorPortAllocation metric so that
 *    NAT exhaustion or AZ-level trouble triggers an alert.
 */

import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import * as sns from "aws-cdk-lib/aws-sns";
import { NetworkStack } from "../lib/stacks/network-stack";

function buildTestApp(): { template: Template } {
  const app = new cdk.App();

  const depsStack = new cdk.Stack(app, "deps", {
    env: { account: "123456789012", region: "us-east-1" },
  });

  const alarmTopic = new sns.Topic(depsStack, "AlarmTopic", {
    topicName: "goosetown-dev-alarms",
  });

  const stack = new NetworkStack(app, "goosetown-dev-network", {
    stackName: "goosetown-dev-network",
    environment: "dev",
    env: { account: "123456789012", region: "us-east-1" },
    alarmTopic,
  });

  return { template: Template.fromStack(stack) };
}

describe("NetworkStack — NAT gateway count", () => {
  let template: Template;

  beforeAll(() => {
    ({ template } = buildTestApp());
  });

  test("exactly 1 NAT gateway is created (multi-NAT deferred to issue #165)", () => {
    // CDK creates one AWS::EC2::NatGateway per NAT. With maxAzs=2 and
    // natGateways=1, only 1 NatGateway resource should appear.
    template.resourceCountIs("AWS::EC2::NatGateway", 1);
  });
});

describe("NetworkStack — NAT port-allocation alarm", () => {
  let template: Template;

  beforeAll(() => {
    ({ template } = buildTestApp());
  });

  test("alarm exists with MetricName ErrorPortAllocation", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      MetricName: "ErrorPortAllocation",
    });
  });

  test("alarm targets the AWS/NATGateway namespace", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      Namespace: "AWS/NATGateway",
    });
  });

  test("alarm uses GreaterThanOrEqualToThreshold (fires on any error)", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      ComparisonOperator: "GreaterThanOrEqualToThreshold",
    });
  });

  test("alarm threshold is 1", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      Threshold: 1,
    });
  });

  test("alarm treats missing data as NOT breaching (NAT with no traffic = healthy)", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      TreatMissingData: "notBreaching",
    });
  });

  test("alarm has an SNS action wired", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmActions: Match.arrayWith([Match.anyValue()]),
    });
  });
});
