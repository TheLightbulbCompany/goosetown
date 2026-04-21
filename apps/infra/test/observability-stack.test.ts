/**
 * observability-stack.test.ts
 *
 * Asserts that ObservabilityStack creates an SNS topic with the expected
 * naming convention and no pre-wired subscriptions (subscriptions are added
 * manually per environment via `aws sns subscribe`).
 */

import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { ObservabilityStack } from "../lib/stacks/observability-stack";

function buildTestApp(env: string): { template: Template } {
  const app = new cdk.App();

  const stack = new ObservabilityStack(app, `goosetown-${env}-observability`, {
    stackName: `goosetown-${env}-observability`,
    environment: env,
    env: { account: "123456789012", region: "us-east-1" },
  });

  return { template: Template.fromStack(stack) };
}

describe("ObservabilityStack — SNS topic", () => {
  let template: Template;

  beforeAll(() => {
    ({ template } = buildTestApp("dev"));
  });

  test("exactly one SNS topic is created", () => {
    template.resourceCountIs("AWS::SNS::Topic", 1);
  });

  test("SNS topic name matches goosetown-{env}-alarms pattern", () => {
    template.hasResourceProperties("AWS::SNS::Topic", {
      TopicName: Match.stringLikeRegexp(/goosetown-\w+-alarms/.source),
    });
  });

  test("SNS topic name is goosetown-dev-alarms for dev env", () => {
    template.hasResourceProperties("AWS::SNS::Topic", {
      TopicName: "goosetown-dev-alarms",
    });
  });

  test("no SNS subscriptions are pre-wired (subscriptions are manual per env)", () => {
    // Subscriptions added via CDK would appear as AWS::SNS::Subscription resources.
    // Per spec: "The topic starts with no subscriptions — addSubscription is a manual step."
    template.resourceCountIs("AWS::SNS::Subscription", 0);
  });

  test("AlarmTopicArn CfnOutput exists", () => {
    const outputs = template.findOutputs("AlarmTopicArn");
    expect(Object.keys(outputs).length).toBe(1);
  });

  test("AlarmTopicArn export name follows goosetown-{env}-alarm-topic-arn convention", () => {
    const outputs = template.findOutputs("AlarmTopicArn");
    const output = Object.values(outputs)[0] as any;
    expect(output.Export?.Name).toMatch(/goosetown-\w+-alarm-topic-arn/);
  });
});

describe("ObservabilityStack — prod env", () => {
  let template: Template;

  beforeAll(() => {
    ({ template } = buildTestApp("prod"));
  });

  test("SNS topic name is goosetown-prod-alarms for prod env", () => {
    template.hasResourceProperties("AWS::SNS::Topic", {
      TopicName: "goosetown-prod-alarms",
    });
  });
});
