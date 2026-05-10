import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cw_actions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as sns from "aws-cdk-lib/aws-sns";
import { DockerImageAsset, Platform } from "aws-cdk-lib/aws-ecr-assets";
import * as path from "path";
import { Construct } from "constructs";
import { AuthSecrets } from "./auth-stack";

export interface WorldgmStackProps extends cdk.StackProps {
  environment: string;
  cluster: ecs.ICluster;
  vpc: ec2.IVpc;
  /** Auth secrets subset needed by worldgm (Clerk + town token for any DDB access via TownService). */
  secrets: AuthSecrets;
  /** SNS topic for CloudWatch alarm actions. */
  alarmTopic?: sns.ITopic;
  /** EventBridge world bus ARN (from EventingStack). */
  worldBusArn: string;
}

/**
 * WorldGM ECS service — single-replica Fargate service that runs the
 * Concordia-powered narrative GameMaster.
 *
 * Reuses the existing backend Docker image (same `apps/backend/` directory)
 * but overrides the container command to `python -m apps.backend.worldgm`.
 * No ALB — this service does not accept inbound HTTP traffic.
 *
 * Singleton by design: Concordia's interrupt-driven GameMaster is stateful
 * in-memory between ticks. If the task restarts, durable arc state survives
 * in DynamoDB (worldgm-arcs table); only the in-flight tick queue is lost.
 */
export class WorldgmStack extends cdk.Stack {
  public readonly service: ecs.FargateService;

  constructor(scope: Construct, id: string, props: WorldgmStackProps) {
    super(scope, id, props);
    const env = props.environment;

    // Reuse the backend Docker image — same Dockerfile, different entrypoint.
    // CDK deduplicates Docker builds by directory hash: if ComputeStack builds
    // the same directory in the same synth, only one image is pushed.
    const backendImage = new DockerImageAsset(this, "WorldgmImage", {
      directory: path.join(__dirname, "..", "..", "..", "backend"),
      platform: Platform.LINUX_AMD64,
    });

    const logGroup = new logs.LogGroup(this, "LogGroup", {
      logGroupName: `/ecs/goosetown-${env}-worldgm`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const taskDef = new ecs.FargateTaskDefinition(this, "TaskDef", {
      cpu: 512,
      memoryLimitMiB: 1024,
    });

    taskDef.addContainer("WorldGm", {
      image: ecs.ContainerImage.fromDockerImageAsset(backendImage),
      // Override entrypoint to start the WorldGM service instead of uvicorn.
      command: ["python", "-m", "apps.backend.worldgm"],
      logging: ecs.LogDrivers.awsLogs({
        logGroup,
        streamPrefix: "worldgm",
      }),
      environment: {
        AWS_REGION: cdk.Stack.of(this).region,
        ENVIRONMENT: env,
        DYNAMODB_TABLE_PREFIX: `goosetown-${env}`,
        WORLD_BUS_NAME: `goosetown-${env}-world`,
        // Tick interval in seconds. Can be overridden per-env without
        // a code deploy by updating the task definition environment.
        WORLDGM_TICK_INTERVAL: "30",
      },
      secrets: {
        CLERK_ISSUER: ecs.Secret.fromSecretsManager(props.secrets.clerkIssuer),
        CLERK_SECRET_KEY: ecs.Secret.fromSecretsManager(props.secrets.clerkSecretKey),
        TOWN_TOKEN_SECRET: ecs.Secret.fromSecretsManager(props.secrets.townTokenSecret),
      },
    });

    // ─── IAM: Bedrock — narrative generation (Nova Lite) + goal checks (Nova Micro) ──
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel", "bedrock:Converse"],
        resources: [
          `arn:aws:bedrock:${cdk.Stack.of(this).region}::foundation-model/amazon.nova-lite-v1:0`,
          `arn:aws:bedrock:${cdk.Stack.of(this).region}::foundation-model/amazon.nova-micro-v1:0`,
        ],
      }),
    );

    // ─── IAM: EventBridge — publish world events ──────────────────────────
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["events:PutEvents"],
        resources: [props.worldBusArn],
      }),
    );

    // ─── IAM: SQS — per-agent queue lifecycle ────────────────────────────
    // Per-agent queues are named `goosetown-{env}-agent-{agentId}`.
    // WorldGM needs send+create access; the backend drainer handles receive+delete.
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          "sqs:CreateQueue",
          "sqs:DeleteQueue",
          "sqs:GetQueueUrl",
          "sqs:GetQueueAttributes",
          "sqs:SendMessage",
          "sqs:SetQueueAttributes",
        ],
        resources: [
          `arn:aws:sqs:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:goosetown-${env}-agent-*`,
        ],
      }),
    );

    // ─── IAM: EventBridge rules — create/delete per-agent routing rules ──
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: [
          "events:PutRule",
          "events:PutTargets",
          "events:DeleteRule",
          "events:RemoveTargets",
          "events:DescribeRule",
          "events:ListTargetsByRule",
        ],
        resources: [
          `arn:aws:events:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:rule/${`goosetown-${env}-world`}/goosetown-${env}-agent-*`,
        ],
      }),
    );

    this.service = new ecs.FargateService(this, "Service", {
      serviceName: `goosetown-${env}-worldgm`,
      cluster: props.cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      assignPublicIp: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      circuitBreaker: { rollback: true },
    });

    // ─── CloudWatch alarm: WorldGM task missing ───────────────────────────
    // Same pattern as GameServerStack: alarm on AWS/ECS:CPUUtilization
    // SampleCount (Container Insights is disabled cluster-wide for cost).
    // WorldGM restart is soft — durable arc state survives in DDB, but
    // narrative events stop firing until the task restarts. The 5-minute
    // evaluation window (one eval per minute) avoids paging on transient
    // task replacement during rolling deploys.
    if (props.alarmTopic) {
      const taskMissingAlarm = new cloudwatch.Alarm(this, "WorldgmTaskMissing", {
        alarmName: `goosetown-${env}-worldgm-task-missing`,
        metric: new cloudwatch.Metric({
          namespace: "AWS/ECS",
          metricName: "CPUUtilization",
          dimensionsMap: {
            ClusterName: props.cluster.clusterName,
            ServiceName: this.service.serviceName,
          },
          statistic: "SampleCount",
          period: cdk.Duration.minutes(1),
        }),
        threshold: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        evaluationPeriods: 5,
        treatMissingData: cloudwatch.TreatMissingData.BREACHING,
        alarmDescription:
          "WorldGM service has no running task. Narrative events stop firing. See RUNBOOK.md.",
      });
      taskMissingAlarm.addAlarmAction(new cw_actions.SnsAction(props.alarmTopic));
    }

    new cdk.CfnOutput(this, "ServiceName", { value: this.service.serviceName });
    new cdk.CfnOutput(this, "LogGroupName", { value: logGroup.logGroupName });
  }
}
