/**
 * sprite-worker-stack.test.ts
 *
 * Regression tests for SpriteWorkerStack (durable sprite pipeline,
 * spec 2026-06-11). Asserts queue config, DLQ wiring, ECS service shape,
 * container command, and key environment variables.
 */

import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as sns from "aws-cdk-lib/aws-sns";
import { SpriteWorkerStack } from "../lib/stacks/sprite-worker-stack";

// ---------------------------------------------------------------------------
// Helpers — minimal dependency stubs so CDK synthesizes without real lookups.
// ---------------------------------------------------------------------------

function buildTestApp(env: string, withAlarmTopic = false): Template {
  const app = new cdk.App({
    context: {
      // Suppress asset bundling during synth
      "aws:cdk:bundling-stacks": [],
    },
  });

  const depsStack = new cdk.Stack(app, `deps-${env}`, {
    env: { account: "123456789012", region: "us-east-1" },
  });

  const vpc = ec2.Vpc.fromVpcAttributes(depsStack, "Vpc", {
    vpcId: "vpc-12345",
    availabilityZones: ["us-east-1a", "us-east-1b"],
    privateSubnetIds: ["subnet-private-1a", "subnet-private-1b"],
    publicSubnetIds: ["subnet-public-1a", "subnet-public-1b"],
  });

  const cluster = new ecs.Cluster(depsStack, "FixtureCluster", {
    vpc,
    clusterName: `goosetown-${env}`,
    containerInsightsV2: ecs.ContainerInsights.DISABLED,
  });

  const spriteBucket = s3.Bucket.fromBucketName(
    depsStack,
    "SpriteBucket",
    `goosetown-${env}-sprites`
  );

  const makeTable = (name: string) =>
    dynamodb.Table.fromTableName(depsStack, name, `goosetown-${env}-${name}`);

  // SpriteWorkerStack only needs props.tables.agents directly; pass the same
  // full shape so `as any` cast on DatabaseTables remains consistent.
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
    clerkIssuer: makeSecret("ClerkIssuer", `goosetown-${env}-clerk-issuer`),
    clerkSecretKey: makeSecret("ClerkSecretKey", `goosetown-${env}-clerk-secret-key`),
    townTokenSecret: makeSecret("TownTokenSecret", `goosetown-${env}-town-token-secret`),
    pixelLabApiKey: makeSecret("PixelLabApiKey", `goosetown-${env}-pixellab-api-key`),
    resendApiKey: makeSecret("ResendApiKey", `goosetown-${env}-resend-api-key`),
    spriteEventSecret: makeSecret(
      "SpriteEventSecret",
      `goosetown-${env}-sprite-event-secret`
    ),
  };

  const alarmTopic = withAlarmTopic
    ? new sns.Topic(depsStack, "AlarmTopic", {
        topicName: `goosetown-${env}-alarms`,
      })
    : undefined;

  const stack = new SpriteWorkerStack(app, `goosetown-${env}-sprite-worker`, {
    stackName: `goosetown-${env}-sprite-worker`,
    environment: env,
    env: { account: "123456789012", region: "us-east-1" },
    vpc,
    cluster,
    tables: tables as any,
    spriteBucket,
    secrets: secrets as any,
    alarmTopic,
  });

  return Template.fromStack(stack);
}

// ---------------------------------------------------------------------------
// SQS queue — name, visibility timeout, DLQ
// ---------------------------------------------------------------------------

describe("SpriteWorkerStack — SQS queue config", () => {
  let template: Template;

  beforeAll(() => {
    template = buildTestApp("dev");
  });

  test("main queue is named goosetown-dev-sprite-jobs", () => {
    template.hasResourceProperties("AWS::SQS::Queue", {
      QueueName: "goosetown-dev-sprite-jobs",
    });
  });

  test("main queue has VisibilityTimeout of 300 seconds", () => {
    template.hasResourceProperties("AWS::SQS::Queue", {
      QueueName: "goosetown-dev-sprite-jobs",
      VisibilityTimeout: 300,
    });
  });

  test("DLQ exists with name goosetown-dev-sprite-jobs-dlq", () => {
    template.hasResourceProperties("AWS::SQS::Queue", {
      QueueName: "goosetown-dev-sprite-jobs-dlq",
    });
  });

  test("main queue has RedrivePolicy with maxReceiveCount 3", () => {
    // CDK encodes the DLQ redrive as a CFn RedrivePolicy property on the
    // main queue. maxReceiveCount is the retry budget before dead-lettering.
    const queues = template.findResources("AWS::SQS::Queue");
    const mainQueue = Object.values(queues).find(
      (q: any) => q?.Properties?.QueueName === "goosetown-dev-sprite-jobs"
    ) as any;
    expect(mainQueue).toBeDefined();
    const redrive = mainQueue?.Properties?.RedrivePolicy;
    expect(redrive).toBeDefined();
    expect(redrive?.maxReceiveCount).toBe(3);
  });

  test("main queue uses long-polling (ReceiveMessageWaitTimeSeconds 20)", () => {
    template.hasResourceProperties("AWS::SQS::Queue", {
      QueueName: "goosetown-dev-sprite-jobs",
      ReceiveMessageWaitTimeSeconds: 20,
    });
  });

  test("main queue retains messages 4 days, DLQ 14 days", () => {
    template.hasResourceProperties("AWS::SQS::Queue", {
      QueueName: "goosetown-dev-sprite-jobs",
      MessageRetentionPeriod: 4 * 24 * 3600,
    });
    template.hasResourceProperties("AWS::SQS::Queue", {
      QueueName: "goosetown-dev-sprite-jobs-dlq",
      MessageRetentionPeriod: 14 * 24 * 3600,
    });
  });
});

// ---------------------------------------------------------------------------
// ECS service — single replica
// ---------------------------------------------------------------------------

describe("SpriteWorkerStack — ECS service", () => {
  let template: Template;

  beforeAll(() => {
    template = buildTestApp("dev");
  });

  test("service has DesiredCount of 1", () => {
    template.hasResourceProperties("AWS::ECS::Service", {
      ServiceName: "goosetown-dev-sprite-worker",
      DesiredCount: 1,
    });
  });

  test("deploys are stop-then-start (min 0% / max 100%) so two workers never overlap", () => {
    template.hasResourceProperties("AWS::ECS::Service", {
      ServiceName: "goosetown-dev-sprite-worker",
      // CDK's maxHealthyPercent renders as the CFn MaximumPercent property.
      DeploymentConfiguration: Match.objectLike({
        MinimumHealthyPercent: 0,
        MaximumPercent: 100,
      }),
    });
  });

  test("service has circuit-breaker rollback enabled", () => {
    template.hasResourceProperties("AWS::ECS::Service", {
      ServiceName: "goosetown-dev-sprite-worker",
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
// Container — command and key environment variables
// ---------------------------------------------------------------------------

describe("SpriteWorkerStack — container definition", () => {
  let template: Template;

  beforeAll(() => {
    template = buildTestApp("dev");
  });

  test("container command is ['uv','run','python','-m','sprite_worker']", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Command: ["uv", "run", "python", "-m", "sprite_worker"],
        }),
      ]),
    });
  });

  test("container environment includes SPRITE_JOBS_QUEUE_NAME", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({
              Name: "SPRITE_JOBS_QUEUE_NAME",
              Value: "goosetown-dev-sprite-jobs",
            }),
          ]),
        }),
      ]),
    });
  });

  test("container environment includes BACKEND_INTERNAL_URL", () => {
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({
              Name: "BACKEND_INTERNAL_URL",
              Value: "https://api-dev.goosetown.isol8.co",
            }),
          ]),
        }),
      ]),
    });
  });

  test("container secrets include PIXELLAB_API_KEY and SPRITE_EVENT_SECRET", () => {
    // SPRITE_EVENT_SECRET authenticates the worker's POSTs to the backend's
    // /town/internal/sprite-event endpoint (codex P2: this seam gets REAL
    // shared-secret auth, unlike the vacuous WS_LAMBDA_SECRET gate).
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Secrets: Match.arrayWith([
            Match.objectLike({ Name: "PIXELLAB_API_KEY" }),
            Match.objectLike({ Name: "SPRITE_EVENT_SECRET" }),
          ]),
        }),
      ]),
    });
  });

  test("BACKEND_INTERNAL_URL uses prod domain for prod env", () => {
    const prodTemplate = buildTestApp("prod");
    prodTemplate.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({
              Name: "BACKEND_INTERNAL_URL",
              Value: "https://api-prod.goosetown.isol8.co",
            }),
          ]),
        }),
      ]),
    });
  });

  test("PixelLab animation concurrency is per-env: dev 1, prod 2 (shared account)", () => {
    // Dev and prod share ONE PixelLab account (no key split, decision
    // 2026-06-12), so the two workers' combined concurrency must respect
    // the account-level concurrent-job cap.
    template.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({ Name: "PIXELLAB_MAX_CONCURRENT_ANIMATIONS", Value: "1" }),
          ]),
        }),
      ]),
    });
    const prodTemplate = buildTestApp("prod");
    prodTemplate.hasResourceProperties("AWS::ECS::TaskDefinition", {
      ContainerDefinitions: Match.arrayWith([
        Match.objectLike({
          Environment: Match.arrayWith([
            Match.objectLike({ Name: "PIXELLAB_MAX_CONCURRENT_ANIMATIONS", Value: "2" }),
          ]),
        }),
      ]),
    });
  });
});

// ---------------------------------------------------------------------------
// Alarms — only synthesized when alarmTopic is provided
// ---------------------------------------------------------------------------

describe("SpriteWorkerStack — CloudWatch alarms", () => {
  test("DLQ-not-empty alarm is created when alarmTopic is provided", () => {
    const template = buildTestApp("dev", true);
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "goosetown-dev-sprite-jobs-dlq-not-empty",
    });
  });

  test("backlog-age alarm is created when alarmTopic is provided", () => {
    const template = buildTestApp("dev", true);
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "goosetown-dev-sprite-jobs-backlog-age",
    });
  });

  test("task-missing alarm is created when alarmTopic is provided", () => {
    const template = buildTestApp("dev", true);
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "goosetown-dev-sprite-worker-task-missing",
      Namespace: "AWS/ECS",
      Statistic: "SampleCount",
      TreatMissingData: "breaching",
    });
  });

  test("PixelLab credit-exhausted alarm is created when alarmTopic is provided", () => {
    const template = buildTestApp("dev", true);
    template.hasResourceProperties("AWS::CloudWatch::Alarm", {
      AlarmName: "goosetown-dev-pixellab-credit-exhausted",
      Namespace: "GooseTown/SpriteWorker",
      MetricName: "CreditExhausted",
    });
  });

  test("no alarms are synthesized when alarmTopic is absent", () => {
    const template = buildTestApp("dev", false);
    const alarms = template.findResources("AWS::CloudWatch::Alarm");
    expect(Object.keys(alarms).length).toBe(0);
  });
});
