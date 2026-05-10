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

// ---------------------------------------------------------------------------
// PixelLab credit-balance poller (Tier 1 #1.1, 2026-05-04 launch-readiness)
// ---------------------------------------------------------------------------

describe("ObservabilityStack — PixelLab credits poller", () => {
  let template: Template;

  beforeAll(() => {
    ({ template } = buildTestApp("dev"));
  });

  test("Lambda function is created with the deterministic name", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "goosetown-dev-pixellab-credits-poller",
      Runtime: "python3.12",
      Handler: "index.handler",
    });
  });

  test("Lambda has the correct env vars wired", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: "goosetown-dev-pixellab-credits-poller",
      Environment: Match.objectLike({
        Variables: Match.objectLike({
          ENVIRONMENT: "dev",
          // Slash-and-underscore form — matches AuthStack's convention for
          // all five auth secrets. See observability-stack.ts comment for
          // the regression history (hyphenated form silently broke the
          // poller from creation until 2026-05-07).
          PIXELLAB_API_KEY_SECRET_NAME: "goosetown/dev/pixellab_api_key",
        }),
      }),
    });
  });

  test("EventBridge rule schedules the Lambda every 15 minutes", () => {
    template.hasResourceProperties("AWS::Events::Rule", {
      Name: "goosetown-dev-pixellab-credits-poll",
      ScheduleExpression: "rate(15 minutes)",
    });
  });

  test("CloudWatch alarm fires on the GooseTown/PixelLabCreditsRemaining metric", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "goosetown-dev-pixellab-credits-low",
      Namespace: "GooseTown",
      MetricName: "PixelLabCreditsRemaining",
      Dimensions: [
        { Name: "Environment", Value: "dev" },
      ],
      ComparisonOperator: "LessThanOrEqualToThreshold",
      // 1000 default threshold, evaluated 2-of-3 to require sustained low.
      Threshold: 1000,
      EvaluationPeriods: 3,
      DatapointsToAlarm: 2,
      // Defensive: missing data = ALARM, so a poller bug pages on-call.
      TreatMissingData: "breaching",
    });
  });

  test("Alarm publishes to the shared SNS topic", () => {
    // Inspect AlarmActions to confirm the SNS topic ARN is referenced
    // (CloudFormation uses Fn::GetAtt for the topic ARN).
    const alarms = template.findResources("AWS::CloudWatch::Alarm", {
      Properties: { AlarmName: "goosetown-dev-pixellab-credits-low" },
    });
    expect(Object.keys(alarms).length).toBe(1);
    const alarm = Object.values(alarms)[0] as any;
    expect(alarm.Properties.AlarmActions).toBeDefined();
    expect(alarm.Properties.AlarmActions.length).toBe(1);
  });

  test("Configurable threshold takes effect when prop is set", () => {
    const app = new cdk.App();
    const stack = new ObservabilityStack(app, "goosetown-dev-observability", {
      stackName: "goosetown-dev-observability",
      environment: "dev",
      env: { account: "123456789012", region: "us-east-1" },
      pixelLabCreditAlarmThreshold: 500,
    });
    const t = Template.fromStack(stack);
    t.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "goosetown-dev-pixellab-credits-low",
      Threshold: 500,
    });
  });
});

// ---------------------------------------------------------------------------
// WorldGM CloudWatch alarms (Plan 3 — Phase E2)
// ---------------------------------------------------------------------------

describe("ObservabilityStack — WorldGM alarms", () => {
  let template: Template;

  beforeAll(() => {
    ({ template } = buildTestApp("dev"));
  });

  test("LLM cost alarm fires on AWS/Bedrock InputTokenCount (Nova Lite)", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "goosetown-dev-worldgm-llm-cost-daily",
      Namespace: "AWS/Bedrock",
      MetricName: "InputTokenCount",
      Dimensions: [{ Name: "ModelId", Value: "amazon.nova-lite-v1:0" }],
      Statistic: "Sum",
      // 1.5 M tokens/hr ≈ $0.20/hr ≈ $5/day at Nova Lite pricing
      Threshold: 1_500_000,
      ComparisonOperator: "GreaterThanThreshold",
      // 6-of-24 hours to require sustained spend
      EvaluationPeriods: 24,
      DatapointsToAlarm: 6,
      TreatMissingData: "notBreaching",
    });
  });

  test("LLM cost alarm publishes to the shared SNS topic", () => {
    const alarms = template.findResources("AWS::CloudWatch::Alarm", {
      Properties: { AlarmName: "goosetown-dev-worldgm-llm-cost-daily" },
    });
    expect(Object.keys(alarms).length).toBe(1);
    const alarm = Object.values(alarms)[0] as any;
    expect(alarm.Properties.AlarmActions).toBeDefined();
    expect(alarm.Properties.AlarmActions.length).toBe(1);
  });

  test("event-rate alarm fires on GooseTown/WorldGM EventsEmitted", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "goosetown-dev-worldgm-event-rate",
      Namespace: "GooseTown/WorldGM",
      MetricName: "EventsEmitted",
      Dimensions: [{ Name: "Environment", Value: "dev" }],
      Statistic: "Sum",
      Threshold: 100,
      ComparisonOperator: "GreaterThanThreshold",
      EvaluationPeriods: 1,
      TreatMissingData: "notBreaching",
    });
  });

  test("event-rate alarm publishes to the shared SNS topic", () => {
    const alarms = template.findResources("AWS::CloudWatch::Alarm", {
      Properties: { AlarmName: "goosetown-dev-worldgm-event-rate" },
    });
    expect(Object.keys(alarms).length).toBe(1);
    const alarm = Object.values(alarms)[0] as any;
    expect(alarm.Properties.AlarmActions).toBeDefined();
    expect(alarm.Properties.AlarmActions.length).toBe(1);
  });

  test("arc-rate alarm fires on GooseTown/WorldGM ArcsKickoff", () => {
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "goosetown-dev-worldgm-arc-rate",
      Namespace: "GooseTown/WorldGM",
      MetricName: "ArcsKickoff",
      Dimensions: [{ Name: "Environment", Value: "dev" }],
      Statistic: "Sum",
      Threshold: 10,
      ComparisonOperator: "GreaterThanThreshold",
      EvaluationPeriods: 1,
      TreatMissingData: "notBreaching",
    });
  });

  test("arc-rate alarm publishes to the shared SNS topic", () => {
    const alarms = template.findResources("AWS::CloudWatch::Alarm", {
      Properties: { AlarmName: "goosetown-dev-worldgm-arc-rate" },
    });
    expect(Object.keys(alarms).length).toBe(1);
    const alarm = Object.values(alarms)[0] as any;
    expect(alarm.Properties.AlarmActions).toBeDefined();
    expect(alarm.Properties.AlarmActions.length).toBe(1);
  });
});
