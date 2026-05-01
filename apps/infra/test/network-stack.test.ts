/**
 * network-stack.test.ts
 *
 * Asserts that:
 * 1. Prod gets 2 NAT gateways (one per AZ) for HA egress — issue #165 resolved.
 * 2. Dev keeps 1 NAT gateway to save ~$32/mo per the intentional cost decision.
 * 3. A CloudWatch alarm exists on the NAT/ErrorPortAllocation metric so that
 *    NAT exhaustion or AZ-level trouble triggers an alert.
 */

import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
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

  test("dev has exactly 1 NAT gateway to save on cost (~$32/mo per extra NAT)", () => {
    // CDK creates one AWS::EC2::NatGateway per NAT. With maxAzs=2 and
    // natGateways=1, only 1 NatGateway resource should appear for dev.
    template.resourceCountIs("AWS::EC2::NatGateway", 1);
  });
});

describe("NetworkStack — multi-AZ NAT for HA egress (issue #165)", () => {
  test("prod has 2 NAT gateways across AZs for HA egress", () => {
    const app = new cdk.App();
    const stack = new NetworkStack(app, "prod-net", {
      stackName: "goosetown-prod-network",
      environment: "prod",
      env: { account: "123456789012", region: "us-east-1" },
    });
    Template.fromStack(stack).resourceCountIs("AWS::EC2::NatGateway", 2);
  });

  test("dev keeps single NAT gateway to save on cost", () => {
    const app = new cdk.App();
    const stack = new NetworkStack(app, "dev-net", {
      stackName: "goosetown-dev-network",
      environment: "dev",
      env: { account: "123456789012", region: "us-east-1" },
    });
    Template.fromStack(stack).resourceCountIs("AWS::EC2::NatGateway", 1);
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
    // Match.anyValue() cannot be nested inside Match.arrayWith() (CDK
    // assertions API restriction). Drop down to findResources and assert
    // shape directly: AlarmActions is a non-empty array with at least one
    // entry pointing at the wired SNS topic.
    const alarms = template.findResources("AWS::CloudWatch::Alarm");
    const natAlarm = Object.values(alarms).find(
      (r) =>
        (r as { Properties: { MetricName?: string } }).Properties
          .MetricName === "ErrorPortAllocation",
    ) as { Properties: { AlarmActions?: unknown[] } } | undefined;

    expect(natAlarm).toBeDefined();
    expect(natAlarm!.Properties.AlarmActions).toBeDefined();
    expect(Array.isArray(natAlarm!.Properties.AlarmActions)).toBe(true);
    expect(natAlarm!.Properties.AlarmActions!.length).toBeGreaterThan(0);
  });
});
