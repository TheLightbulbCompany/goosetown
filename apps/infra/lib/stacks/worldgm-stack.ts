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
import { DatabaseTables } from "./database-stack";

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
  /** DynamoDB tables (from DatabaseStack) the worldgm tick loop reads/writes. */
  tables: DatabaseTables;
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
      // 1 vCPU (was 0.5): the agentic resident brain runs ~20 concurrent Bedrock
      // calls per tick in the same process as the 20 in-process resident
      // daemons. On 0.5 vCPU the boto3 CPU work under the GIL starved the
      // daemons' WebSocket loops, causing inactivity-timeout reconnect churn
      // (which interrupted activities so mood never updated). 1 vCPU + 2 GB
      // gives the event loop headroom. Brain concurrency is also capped via
      // WORLDGM_MAX_CONCURRENT_DECISIONS.
      cpu: 1024,
      memoryLimitMiB: 2048,
    });

    taskDef.addContainer("WorldGm", {
      image: ecs.ContainerImage.fromDockerImageAsset(backendImage),
      // Override entrypoint to start the WorldGM service instead of uvicorn.
      // The Docker WORKDIR is /app (set by apps/backend/Dockerfile); the
      // ``worldgm/`` package lives directly under it, not under ``apps.backend.``.
      // Use ``uv run`` to match the rest of the image's invocation pattern
      // (the default CMD is ``uv run uvicorn ...``).
      command: ["uv", "run", "python", "-m", "worldgm"],
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
        // Public WebSocket URL the resident daemons connect through (same as
        // the backend's TOWN_WS_URL — residents run inside AWS but connect via
        // the API-Gateway WS exactly like remote agents).
        TOWN_WS_URL:
          env === "prod"
            ? "wss://ws-prod.goosetown.isol8.co"
            : "wss://ws-dev.goosetown.isol8.co",
        // Resident (seed) agents. Enabled on both envs (prod seeding decided
        // 2026-06-11). The slate differs: dev runs the historical/archetype
        // cast; prod runs the real-public-figures cast (personas_prod.py).
        RESIDENTS_ENABLED: "true",
        RESIDENT_SLATE: env === "prod" ? "prod" : "default",
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

    // ─── IAM: CloudWatch — emit custom GooseTown/WorldGM metrics ─────────
    // PutMetricData has no resource-level scoping in IAM; the namespace
    // dimension (GooseTown/WorldGM) is enforced by convention only.
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
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

    // ─── IAM: DynamoDB — the tick loop's table access ───────────────────
    // Without these grants every tick fails with AccessDeniedException and
    // the WorldGM is a silent no-op (the only failure mode that doesn't
    // trip the task-missing alarm — the task stays "running" while doing
    // nothing). Scoped to exactly what the tick loop touches:
    //   - agents: read+write. Read: sync_locations → get_active_agents queries
    //     the isActive-joinedAt-index GSI. Write: resident provisioning —
    //     create_resident PutItem + update_agent_state UpdateItem (reactivate
    //     on start / deactivate on stop). grantReadWriteData covers table +
    //     /index/*. (Read-only here crash-loops resident daemons on PutItem.)
    //   - worldgm-arcs: read+write (arc_orchestrator query/scan/put/update)
    //   - world-events: write (event_publisher put_item)
    //   - conversations: read — world_snapshot.build_snapshot reads recent
    //     conversations (Query on status-startedAt-index) as a GM X-factor.
    //   - relationships: read+write — build_snapshot resolves affinity via
    //     get_or_create_relationship (GetItem + PutItem of a default row for a
    //     never-seen pair). grantReadWriteData covers table + /index/*.
    props.tables.agents.grantReadWriteData(taskDef.taskRole);
    props.tables.worldgmArcs.grantReadWriteData(taskDef.taskRole);
    props.tables.worldEvents.grantWriteData(taskDef.taskRole);
    props.tables.conversations.grantReadData(taskDef.taskRole);
    props.tables.relationships.grantReadWriteData(taskDef.taskRole);

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
