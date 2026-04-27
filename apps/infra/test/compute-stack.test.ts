/**
 * compute-stack.test.ts
 *
 * Regression tests for ComputeStack. The most critical assertion is that
 * the synthesized template contains ZERO Fn::ImportValue references to
 * "management-api-url". That cross-stack import caused fresh-env deploys
 * to fail (ComputeStack deploys before ApiStack, so the export didn't exist).
 *
 * Also asserts per-env rolling-update config and circuit-breaker rollback.
 */

import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { ComputeStack } from "../lib/stacks/compute-stack";

// ---------------------------------------------------------------------------
// Helpers — create minimal dependency stubs inside a dedicated "deps" stack
// so ComputeStack can synthesize without real AWS lookups.
// ---------------------------------------------------------------------------

function buildTestApp(env: string): {
  app: cdk.App;
  computeStack: ComputeStack;
  template: Template;
} {
  const app = new cdk.App({
    context: {
      // Suppress asset bundling during synth
      "aws:cdk:bundling-stacks": [],
    },
  });

  // Deps stack in a known account/region so CDK doesn't need real lookups.
  const depsStack = new cdk.Stack(app, `deps-${env}`, {
    env: { account: "123456789012", region: "us-east-1" },
  });

  // Minimal VPC — use fromVpcAttributes to avoid EC2 DescribeVpcs lookup.
  const vpc = ec2.Vpc.fromVpcAttributes(depsStack, "Vpc", {
    vpcId: "vpc-12345",
    availabilityZones: ["us-east-1a", "us-east-1b"],
    privateSubnetIds: ["subnet-private-1a", "subnet-private-1b"],
    publicSubnetIds: ["subnet-public-1a", "subnet-public-1b"],
  });

  // Sprite bucket stub
  const spriteBucket = s3.Bucket.fromBucketName(
    depsStack,
    "SpriteBucket",
    `goosetown-${env}-sprites`
  );

  // DynamoDB table stubs
  const makeTable = (name: string) =>
    dynamodb.Table.fromTableName(depsStack, name, `goosetown-${env}-${name}`);

  const tables = {
    agents: makeTable("agents"),
    instances: makeTable("instances"),
    relationships: makeTable("relationships"),
    conversations: makeTable("conversations"),
    connections: makeTable("connections"),
  };

  // Secrets stubs
  const makeSecret = (logicalId: string, name: string) =>
    secretsmanager.Secret.fromSecretNameV2(depsStack, logicalId, name);

  const secrets = {
    clerkIssuer: makeSecret("ClerkIssuer", `goosetown-${env}-clerk-issuer`),
    clerkSecretKey: makeSecret(
      "ClerkSecretKey",
      `goosetown-${env}-clerk-secret-key`
    ),
    townTokenSecret: makeSecret(
      "TownTokenSecret",
      `goosetown-${env}-town-token-secret`
    ),
    pixelLabApiKey: makeSecret(
      "PixelLabApiKey",
      `goosetown-${env}-pixellab-api-key`
    ),
  };

  const computeStack = new ComputeStack(app, `goosetown-${env}-compute`, {
    stackName: `goosetown-${env}-compute`,
    environment: env,
    env: { account: "123456789012", region: "us-east-1" },
    vpc,
    tables: tables as any,
    spriteBucket,
    secrets: secrets as any,
    // No certificate, hostedZone, gameServerUrl, gameServerSg, alarmTopic
    // — all are optional in ComputeStackProps.
  });

  const template = Template.fromStack(computeStack);
  return { app, computeStack, template };
}

// ---------------------------------------------------------------------------
// CRITICAL: No Fn::ImportValue for management-api-url
// ---------------------------------------------------------------------------

describe("ComputeStack — CRITICAL: no Fn::ImportValue for management-api-url", () => {
  test("dev template contains zero Fn::ImportValue references to management-api-url", () => {
    const { template } = buildTestApp("dev");
    const templateJson = JSON.stringify(template.toJSON());
    // The critical regression guard: this import caused fresh-env deploys to
    // fail because ComputeStack deployed before ApiStack. Any reintroduction
    // of this Fn::ImportValue will break greenfield environments.
    expect(templateJson).not.toContain("management-api-url");
    expect(templateJson).not.toContain("Fn::ImportValue");
  });

  test("prod template contains zero Fn::ImportValue references to management-api-url", () => {
    const { template } = buildTestApp("prod");
    const templateJson = JSON.stringify(template.toJSON());
    expect(templateJson).not.toContain("management-api-url");
    expect(templateJson).not.toContain("Fn::ImportValue");
  });
});

// ---------------------------------------------------------------------------
// Dev: desiredCount=1, minHealthyPercent=0
// ---------------------------------------------------------------------------

describe("ComputeStack — dev rolling-update config", () => {
  let template: Template;

  beforeAll(() => {
    ({ template } = buildTestApp("dev"));
  });

  test("dev ECS service has desiredCount=1", () => {
    template.hasResourceProperties("AWS::ECS::Service", {
      DesiredCount: 1,
    });
  });

  test("dev ECS service has MinimumHealthyPercent=0", () => {
    template.hasResourceProperties("AWS::ECS::Service", {
      DeploymentConfiguration: Match.objectLike({
        MinimumHealthyPercent: 0,
      }),
    });
  });

  test("dev ECS service has circuit breaker rollback enabled", () => {
    template.hasResourceProperties("AWS::ECS::Service", {
      DeploymentConfiguration: Match.objectLike({
        DeploymentCircuitBreaker: {
          Enable: true,
          Rollback: true,
        },
      }),
    });
  });
});

// ---------------------------------------------------------------------------
// Prod: desiredCount=2, minHealthyPercent=100
// ---------------------------------------------------------------------------

describe("ComputeStack — prod rolling-update config", () => {
  let template: Template;

  beforeAll(() => {
    ({ template } = buildTestApp("prod"));
  });

  test("prod ECS service has desiredCount=2", () => {
    template.hasResourceProperties("AWS::ECS::Service", {
      DesiredCount: 2,
    });
  });

  test("prod ECS service has MinimumHealthyPercent=100", () => {
    template.hasResourceProperties("AWS::ECS::Service", {
      DeploymentConfiguration: Match.objectLike({
        MinimumHealthyPercent: 100,
      }),
    });
  });

  test("prod ECS service has circuit breaker rollback enabled", () => {
    template.hasResourceProperties("AWS::ECS::Service", {
      DeploymentConfiguration: Match.objectLike({
        DeploymentCircuitBreaker: {
          Enable: true,
          Rollback: true,
        },
      }),
    });
  });
});

// ---------------------------------------------------------------------------
// Backend-task-missing alarm uses HealthyHostCount (published by ELB)
// not RunningTaskCount (which is only under ECS/ContainerInsights).
// Regression guard: the AWS/ECS RunningTaskCount approach left the alarm
// stuck in ALARM after deploy (see F5 in 2026-04-21 dry run).
// ---------------------------------------------------------------------------

describe("ComputeStack — backend-task-missing alarm metric", () => {
  test("alarm uses HealthyHostCount, not RunningTaskCount", () => {
    // Build a test app that provides alarmTopic so the alarm is synthesized.
    const app = new cdk.App();
    const depsStack = new cdk.Stack(app, "deps-alarm", {
      env: { account: "123456789012", region: "us-east-1" },
    });
    const vpc = ec2.Vpc.fromVpcAttributes(depsStack, "Vpc", {
      vpcId: "vpc-12345",
      availabilityZones: ["us-east-1a", "us-east-1b"],
      privateSubnetIds: ["subnet-private-1a", "subnet-private-1b"],
      publicSubnetIds: ["subnet-public-1a", "subnet-public-1b"],
    });
    const spriteBucket = s3.Bucket.fromBucketName(
      depsStack,
      "SpriteBucket",
      "goosetown-dev-sprites"
    );
    const makeTable = (name: string) =>
      dynamodb.Table.fromTableName(
        depsStack,
        name,
        `goosetown-dev-${name}`
      );
    const tables = {
      agents: makeTable("agents"),
      instances: makeTable("instances"),
      relationships: makeTable("relationships"),
      conversations: makeTable("conversations"),
      connections: makeTable("connections"),
    };
    const makeSecret = (logicalId: string, name: string) =>
      secretsmanager.Secret.fromSecretNameV2(depsStack, logicalId, name);
    const secrets = {
      clerkIssuer: makeSecret("ClerkIssuer", "goosetown-dev-clerk-issuer"),
      clerkSecretKey: makeSecret(
        "ClerkSecretKey",
        "goosetown-dev-clerk-secret-key"
      ),
      townTokenSecret: makeSecret(
        "TownTokenSecret",
        "goosetown-dev-town-token-secret"
      ),
      pixelLabApiKey: makeSecret(
        "PixelLabApiKey",
        "goosetown-dev-pixellab-api-key"
      ),
    };
    const alarmTopic = new (require("aws-cdk-lib/aws-sns").Topic)(
      depsStack,
      "AlarmTopic",
      { topicName: "goosetown-dev-alarms" }
    );
    const computeStack = new ComputeStack(app, "goosetown-dev-compute-alarm", {
      stackName: "goosetown-dev-compute-alarm",
      environment: "dev",
      env: { account: "123456789012", region: "us-east-1" },
      vpc,
      tables: tables as any,
      spriteBucket,
      secrets: secrets as any,
      alarmTopic,
    });
    const template = Template.fromStack(computeStack);
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      MetricName: "HealthyHostCount",
      Namespace: "AWS/ApplicationELB",
    });
    // Extra guard: the old-metric name should NOT appear anywhere in the template.
    const json = JSON.stringify(template.toJSON());
    expect(json).not.toContain('"MetricName":"RunningTaskCount"');
  });
});

// ---------------------------------------------------------------------------
// WS_MANAGEMENT_API_URL is a plain string (not an import token)
// ---------------------------------------------------------------------------

describe("ComputeStack — WS_MANAGEMENT_API_URL env var is a literal string", () => {
  test("dev WS_MANAGEMENT_API_URL resolves to ws-dev custom domain string", () => {
    const { template } = buildTestApp("dev");
    const templateJson = JSON.stringify(template.toJSON());
    // Should contain the custom-domain literal, not a CF intrinsic import
    expect(templateJson).toContain("ws-dev.goosetown.isol8.co");
  });

  test("prod WS_MANAGEMENT_API_URL resolves to ws custom domain string", () => {
    const { template } = buildTestApp("prod");
    const templateJson = JSON.stringify(template.toJSON());
    expect(templateJson).toContain("ws.goosetown.isol8.co");
  });
});

// ---------------------------------------------------------------------------
// Phase 6: ALB access logging
// ---------------------------------------------------------------------------

describe("ComputeStack — ALB access logging", () => {
  test("dev creates an ALB access logs bucket", () => {
    const { template } = buildTestApp("dev");
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: "goosetown-dev-alb-logs",
    });
  });

  test("prod creates an ALB access logs bucket", () => {
    const { template } = buildTestApp("prod");
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: "goosetown-prod-alb-logs",
    });
  });

  test("ALB has access logging enabled", () => {
    const { template } = buildTestApp("dev");
    // CDK encodes the ELB access-log opt-in as
    // ``LoadBalancerAttributes`` entries with key ``access_logs.s3.enabled``.
    template.hasResourceProperties(
      "AWS::ElasticLoadBalancingV2::LoadBalancer",
      {
        LoadBalancerAttributes: Match.arrayWith([
          Match.objectLike({
            Key: "access_logs.s3.enabled",
            Value: "true",
          }),
        ]),
      }
    );
  });

  test("ALB log bucket has 90-day expiry lifecycle rule", () => {
    const { template } = buildTestApp("dev");
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: "goosetown-dev-alb-logs",
      LifecycleConfiguration: Match.objectLike({
        Rules: Match.arrayWith([
          Match.objectLike({
            ExpirationInDays: 90,
            Status: "Enabled",
          }),
        ]),
      }),
    });
  });
});

// ---------------------------------------------------------------------------
// Phase 6: backend autoscaling
// ---------------------------------------------------------------------------

describe("ComputeStack — backend autoscaling", () => {
  test("dev scales 1→3 tasks", () => {
    const { template } = buildTestApp("dev");
    template.hasResourceProperties(
      "AWS::ApplicationAutoScaling::ScalableTarget",
      {
        MinCapacity: 1,
        MaxCapacity: 3,
        ScalableDimension: "ecs:service:DesiredCount",
      }
    );
  });

  test("prod scales 2→6 tasks", () => {
    const { template } = buildTestApp("prod");
    template.hasResourceProperties(
      "AWS::ApplicationAutoScaling::ScalableTarget",
      {
        MinCapacity: 2,
        MaxCapacity: 6,
        ScalableDimension: "ecs:service:DesiredCount",
      }
    );
  });

  test("scaling target uses 60% CPU utilization", () => {
    const { template } = buildTestApp("prod");
    template.hasResourceProperties(
      "AWS::ApplicationAutoScaling::ScalingPolicy",
      {
        PolicyType: "TargetTrackingScaling",
        TargetTrackingScalingPolicyConfiguration: Match.objectLike({
          TargetValue: 60,
          PredefinedMetricSpecification: Match.objectLike({
            PredefinedMetricType: "ECSServiceAverageCPUUtilization",
          }),
        }),
      }
    );
  });
});
