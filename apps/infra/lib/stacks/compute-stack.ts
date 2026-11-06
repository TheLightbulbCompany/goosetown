import { canonicalHost, frontendOrigin, canonicalZoneId, canonicalZoneName } from "./canonical-domain";
import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import { DockerImageAsset, Platform } from "aws-cdk-lib/aws-ecr-assets";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as path from "path";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as route53Targets from "aws-cdk-lib/aws-route53-targets";
import * as sns from "aws-cdk-lib/aws-sns";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cw_actions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as wafv2 from "aws-cdk-lib/aws-wafv2";
import { Construct } from "constructs";
import { DatabaseTables } from "./database-stack";
import { AuthSecrets } from "./auth-stack";

export interface ComputeStackProps extends cdk.StackProps {
  environment: string;
  vpc: ec2.IVpc;
  /** Shared ECS cluster from ClusterStack — both services run on the same cluster. */
  cluster: ecs.ICluster;
  tables: DatabaseTables;
  spriteBucket: s3.IBucket;
  secrets: AuthSecrets;
  certificate?: acm.ICertificate;
  hostedZone?: route53.IHostedZone;
  gameServerUrl?: string;
  gameServerSg?: ec2.ISecurityGroup;
  /** SNS topic for CloudWatch alarm actions. Wired by ObservabilityStack. */
  alarmTopic?: sns.ITopic;
}

export class ComputeStack extends cdk.Stack {
  public readonly alb: elbv2.ApplicationLoadBalancer;
  public readonly service: ecs.FargateService;

  constructor(scope: Construct, id: string, props: ComputeStackProps) {
    super(scope, id, props);

    const env = props.environment;

    // Docker image asset (built & pushed by CDK Pipeline)
    const backendImage = new DockerImageAsset(this, "BackendImage", {
      directory: path.join(__dirname, "..", "..", "..", "backend"),
      platform: Platform.LINUX_AMD64,
    });

    // ECS Cluster — shared with GameServerStack via ClusterStack cross-stack
    // reference. Container Insights is disabled in ClusterStack (cost reason).
    const cluster = props.cluster;

    // Task execution role (ECR pull, CloudWatch logs)
    const executionRole = new iam.Role(this, "ExecutionRole", {
      roleName: `goosetown-${env}-task-execution`,
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          "service-role/AmazonECSTaskExecutionRolePolicy"
        ),
      ],
    });

    // Grant execution role access to secrets (for container env injection)
    Object.entries(props.secrets).forEach(([name, secret]) => {
      if (name !== "clerkSecretKey") secret.grantRead(executionRole);
    });

    // Task role (app-level permissions)
    const taskRole = new iam.Role(this, "TaskRole", {
      roleName: `goosetown-${env}-task`,
      assumedBy: new iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
    });

    // Grant DynamoDB access
    Object.values(props.tables).forEach((table) => {
      table.grantReadWriteData(taskRole);
    });

    // Grant S3 access for sprites
    props.spriteBucket.grantReadWrite(taskRole);

    // Grant secrets read to task role too (for runtime SDK access)
    Object.entries(props.secrets).forEach(([name, secret]) => {
      if (name !== "clerkSecretKey") secret.grantRead(taskRole);
    });

    // Durable sprite pipeline: backend produces to the sprite-jobs queue.
    // ARN string (not a queue object) to avoid a cross-stack dependency on
    // SpriteWorkerStack — same pattern as worldgm's agent-* queue grant.
    taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["sqs:SendMessage", "sqs:GetQueueUrl"],
        resources: [
          `arn:aws:sqs:${cdk.Stack.of(this).region}:${cdk.Stack.of(this).account}:goosetown-${env}-sprite-jobs`,
        ],
      }),
    );

    // CloudWatch log group
    const logGroup = new logs.LogGroup(this, "LogGroup", {
      logGroupName: `/ecs/goosetown-${env}`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Fargate Task Definition
    const taskDefinition = new ecs.FargateTaskDefinition(
      this,
      "TaskDefinition",
      {
        family: `goosetown-${env}-backend`,
        // 1 vCPU (was 0.5): on 0.5 vCPU the event loop saturated under the
        // world_state-broadcast + WS-daemon + GM + sprite-compositing load
        // (CPU spiked to 95-99%), so GET /health exceeded its 5s timeout and the
        // ALB reaped healthy tasks ("unhealthy ... Request timed out" → ECS
        // replaced), churning WS connections and killing in-flight work. Fargate
        // requires >=2GB memory at 1 vCPU. Mirrors the worldgm 0.5->1 bump (#345).
        cpu: 1024,
        memoryLimitMiB: 2048,
        executionRole,
        taskRole,
      }
    );

    const container = taskDefinition.addContainer("backend", {
      image: ecs.ContainerImage.fromDockerImageAsset(backendImage),
      logging: ecs.LogDrivers.awsLogs({
        logGroup,
        streamPrefix: "backend",
      }),
      environment: {
        AWS_REGION: "us-east-1",
        ENVIRONMENT: env,
        CLERK_AUTHORIZED_PARTIES: env === "prod" ? "https://goosetown.thelightbulb.company" : "https://dev.goosetown.thelightbulb.company",
        DYNAMODB_TABLE_PREFIX: `goosetown-${env}`,
        // Exchange timing knobs (core/services/exchange_service.py reads these).
        // Prod keeps the production defaults; dev uses short windows so the
        // full job lifecycle (incl. delivery/review timeouts) is testable in
        // minutes and the multi-agent soak can exercise many loops quickly.
        EXCHANGE_BID_WINDOW_SEC: env === "prod" ? "600" : "90",
        EXCHANGE_DELIVERY_SEC: env === "prod" ? "3600" : "180",
        EXCHANGE_REVIEW_SEC: env === "prod" ? "1800" : "120",
        SPRITE_S3_BUCKET: props.spriteBucket.bucketName,
        SPRITE_CDN_URL: `https://${canonicalHost("assets", env)}`,
        RUNTIME_BINDING_ORIGIN: `https://${canonicalHost("api", env)}`,
        CORS_ORIGINS: env === "prod"
          ? frontendOrigin(env)
          : `${frontendOrigin(env)},http://localhost:3000,http://localhost:5173`,
        ...(props.gameServerUrl && {
          GAME_SERVER_URL: `ws://${props.gameServerUrl}:8080`,
        }),
        TOWN_WS_URL: `wss://${canonicalHost("ws", env)}`,
        TOWN_API_URL: `https://${canonicalHost("api", env)}/api/v1`,
        // Using the custom-domain string avoids an Fn.importValue cross-stack
        // reference from ComputeStack to ApiStack — which would be circular
        // because ApiStack already depends on ComputeStack via fargateTaskRole,
        // and Compute deploys before Api on fresh environments.
        // The ApiStack export (goosetown-${env}-management-api-url) is kept in
        // place for at least one week of clean prod runtime before removal.
        WS_MANAGEMENT_API_URL: `https://${canonicalHost("ws", env)}`,
        // Operator step (one-time, post-merge): after creating Resend audiences
        // (one for dev, one for prod), replace these TBD strings and redeploy.
        // Until populated, /landing/signup returns 503 with a server log
        // explaining the misconfiguration.
        RESEND_AUDIENCE_ID: env === "prod" ? "TBD-prod-audience-id" : "TBD-dev-audience-id",
        // Durable sprite pipeline (spec 2026-06-11): backend is producer-only.
        SPRITE_JOBS_QUEUE_NAME: `goosetown-${env}-sprite-jobs`,
        // Lazy animation generation (activity anims + talking). Dev MUST stay
        // "false" so it never burns the shared PixelLab account on lazy gen.
        ACTIVITY_ANIMATION_GENERATION: env === "prod" ? "true" : "false",
      },
      secrets: {
        CLERK_ISSUER: ecs.Secret.fromSecretsManager(props.secrets.clerkIssuer),
        TOWN_TOKEN_SECRET: ecs.Secret.fromSecretsManager(
          props.secrets.townTokenSecret
        ),
        PIXELLAB_API_KEY: ecs.Secret.fromSecretsManager(
          props.secrets.pixelLabApiKey
        ),
        RESEND_API_KEY: ecs.Secret.fromSecretsManager(props.secrets.resendApiKey),
        // Authenticates the sprite worker's POSTs to /town/internal/sprite-event.
        // Same secret is wired into the sprite worker (sprite-worker-stack).
        SPRITE_EVENT_SECRET: ecs.Secret.fromSecretsManager(
          props.secrets.spriteEventSecret
        ),
        // #427: authenticates the API Gateway WS Lambdas' POSTs to
        // /town/ws-message and /town/ws-disconnect. Same secret is wired into
        // the ws-default / ws-disconnect Lambdas (api-stack).
        WS_LAMBDA_SECRET: ecs.Secret.fromSecretsManager(
          props.secrets.wsLambdaSecret
        ),
      },
      portMappings: [{ containerPort: 8000, protocol: ecs.Protocol.TCP }],
      healthCheck: {
        command: [
          "CMD-SHELL",
          "curl -f http://localhost:8000/health || exit 1",
        ],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(10),
        retries: 3,
        startPeriod: cdk.Duration.seconds(60),
      },
    });

    // ALB
    this.alb = new elbv2.ApplicationLoadBalancer(this, "ALB", {
      loadBalancerName: `goosetown-${env}`,
      vpc: props.vpc,
      internetFacing: true,
      idleTimeout: cdk.Duration.seconds(300),
    });

    // Access logging — needed for forensics on auth probes / rate-limit
    // decisions / WAF tuning. Bucket is per-env, 90-day lifecycle so we
    // don't grow indefinitely. CDK auto-attaches the AWS-managed bucket
    // policy that grants the ELB account write access.
    // Retention is per-env: prod RETAINs (forensics matter), dev DESTROYs
    // so a `cdk destroy` + redeploy doesn't error with
    // ``BucketAlreadyOwnedByYou`` on the fixed bucket name.
    // ``autoDeleteObjects`` only fires when the policy is DESTROY.
    const albLogsBucket = new s3.Bucket(this, "AlbAccessLogsBucket", {
      bucketName: `goosetown-${env}-alb-logs`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      lifecycleRules: [
        {
          id: "expire-old-logs",
          enabled: true,
          expiration: cdk.Duration.days(90),
          noncurrentVersionExpiration: cdk.Duration.days(7),
        },
      ],
      removalPolicy:
        env === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: env !== "prod",
    });
    this.alb.logAccessLogs(albLogsBucket, `alb-${env}`);

    // Target group with sticky sessions for WebSocket support
    const targetGroup = new elbv2.ApplicationTargetGroup(this, "TargetGroup", {
      targetGroupName: `goosetown-${env}-tg`,
      vpc: props.vpc,
      port: 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targetType: elbv2.TargetType.IP,
      healthCheck: {
        path: "/health",
        interval: cdk.Duration.seconds(30),
        // 10s (was 5) + unhealthy threshold 5 (was 3): tolerate transient
        // event-loop busyness so the ALB doesn't reap an otherwise-healthy task
        // (and any in-flight work on it) during a brief CPU spike. Paired with
        // the 0.5->1 vCPU bump above, which is the fundamental fix.
        timeout: cdk.Duration.seconds(10),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 5,
      },
      stickinessCookieDuration: cdk.Duration.hours(1),
    });

    // HTTPS listener if certificate is provided, otherwise HTTP
    if (props.certificate) {
      this.alb.addListener("HttpsListener", {
        port: 443,
        protocol: elbv2.ApplicationProtocol.HTTPS,
        certificates: [props.certificate],
        defaultTargetGroups: [targetGroup],
      });

      // HTTP redirect to HTTPS
      this.alb.addListener("HttpRedirect", {
        port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
        defaultAction: elbv2.ListenerAction.redirect({
          protocol: "HTTPS",
          port: "443",
          permanent: true,
        }),
      });
    } else {
      this.alb.addListener("HttpListener", {
        port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
        defaultTargetGroups: [targetGroup],
      });
    }

    // Fargate Service
    // Desired count remains one. Production rolls with a temporary overlapping
    // task to retain a healthy backend; the dedicated sprite worker owns generation.
    // This is not multi-replica high availability outside a deployment.
    const isProd = env === "prod";
    this.service = new ecs.FargateService(this, "Service", {
      serviceName: `goosetown-${env}-backend`,
      cluster,
      taskDefinition,
      desiredCount: 1,
      assignPublicIp: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      circuitBreaker: { rollback: true },
      minHealthyPercent: 100,
      maxHealthyPercent: 200,
    });

    this.service.attachToApplicationTargetGroup(targetGroup);

    // Auto-scaling on CPU. Prod gets 2-6 tasks (2 baseline for HA, 6 ceiling
    // protects budget on a runaway scrape); dev gets 1-3 (1 baseline so a
    // bad health check can recover via rolling update, 3 ceiling so a
    // misconfigured load test in dev can't accidentally autoscale to prod
    // levels). Target 60% CPU — leaves headroom for sprite-pipeline bursts
    // before sustained breach forces scale-out. Game server stays single-
    // replica until sharding is designed (see gameserver-stack.ts).
    // maxCapacity: prod 12, dev 3. Producer's 2026-05-04 launch-readiness
    // plan (Tier 2 #2.2) raised prod from 6→12 to give headroom for an
    // HN-spike scenario where 500 simultaneous /town/instance + register
    // calls would peg CPU on 6 tasks. Game server stays single-replica
    // until room-sharding (ADR-0002).
    // INTERIM single-task pin (see the FargateService comment above): prod is
    // held at exactly 1 (min=max=1) so the per-process SpriteQueue throttle is
    // also the global one. Dev keeps 1-3 headroom. Restore prod to 2-12 when the
    // durable sprite worker decouples generation from the backend.
    const scaling = this.service.autoScaleTaskCount({
      minCapacity: 1,
      maxCapacity: isProd ? 1 : 3,
    });
    scaling.scaleOnCpuUtilization("CpuScaling", {
      targetUtilizationPercent: 60,
      scaleInCooldown: cdk.Duration.seconds(60),
      scaleOutCooldown: cdk.Duration.seconds(60),
    });

    // ─── WAF (regional, attached to ALB) ──────────────────────────────────
    // Three managed rule groups + one rate-based rule. The primary attack
    // we're defending against is /town/agent/register triggering PixelLab
    // sprite generation — that's a direct billing vector. Rate-based
    // (2000 req / 5 min / IP) blocks the volumetric abuse path; managed
    // rule groups catch generic exploit attempts. IP-reputation and
    // KnownBadInputs default to BLOCK because they have very low FP rates;
    // CommonRuleSet promoted to BLOCK after >7 days of ALB access-log review
    // with no legitimate-traffic FPs observed (PR #216).
    const albWaf = new wafv2.CfnWebACL(this, "AlbWebAcl", {
      name: `goosetown-${env}-alb-waf`,
      scope: "REGIONAL",
      defaultAction: { allow: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: `goosetown-${env}-alb-waf`,
        sampledRequestsEnabled: true,
      },
      rules: [
        {
          name: "AWSManagedRulesAmazonIpReputationList",
          priority: 0,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: "AWS",
              name: "AWSManagedRulesAmazonIpReputationList",
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "ip-reputation",
            sampledRequestsEnabled: true,
          },
        },
        {
          name: "AWSManagedRulesKnownBadInputsRuleSet",
          priority: 1,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: "AWS",
              name: "AWSManagedRulesKnownBadInputsRuleSet",
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "known-bad-inputs",
            sampledRequestsEnabled: true,
          },
        },
        {
          name: "AWSManagedRulesCommonRuleSet",
          priority: 2,
          // BLOCK mode after >7 days FP review (PR #216). Access logs
          // showed no legitimate traffic matching CommonRuleSet rules;
          // promoted from COUNT to ``none: {}`` (default-BLOCK posture).
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: "AWS",
              name: "AWSManagedRulesCommonRuleSet",
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "common-rules",
            sampledRequestsEnabled: true,
          },
        },
        {
          name: "RateLimit2000Per5Min",
          priority: 3,
          action: { block: {} },
          statement: {
            rateBasedStatement: {
              limit: 2000,
              aggregateKeyType: "IP",
              evaluationWindowSec: 300,
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "rate-limit",
            sampledRequestsEnabled: true,
          },
        },
      ],
    });

    new wafv2.CfnWebACLAssociation(this, "AlbWebAclAssociation", {
      resourceArn: this.alb.loadBalancerArn,
      webAclArn: albWaf.attrArn,
    });

    // CloudWatch alarm: fire if fewer healthy targets are registered with the
    // ALB than desired for 5+ minutes. We use HealthyHostCount (AWS/ApplicationELB
    // namespace, always published) instead of ECS RunningTaskCount because the
    // latter is only published under ECS/ContainerInsights, which this cluster
    // has disabled for cost reasons (see ecs.Cluster above). HealthyHostCount
    // is a tighter signal anyway — a task can be "running" but failing health
    // checks, which from the user's perspective is the same as missing.
    // Silently skipped when alarmTopic is absent (local synth / CI without observability stack).
    if (props.alarmTopic) {
      const taskMissingAlarm = new cloudwatch.Alarm(this, "BackendTaskMissing", {
        alarmName: `goosetown-${env}-backend-task-missing`,
        metric: targetGroup.metrics.healthyHostCount({
          statistic: "Minimum",
          period: cdk.Duration.minutes(1),
        }),
        // 1 healthy target — both envs run a single task during the interim
        // single-replica pin (see FargateService comment). Restore to `isProd ? 2`
        // when prod HA is restored alongside the durable sprite worker.
        threshold: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        evaluationPeriods: 5,
        treatMissingData: cloudwatch.TreatMissingData.BREACHING,
        alarmDescription:
          "Backend has fewer healthy ALB targets than desired. See RUNBOOK.md.",
      });
      taskMissingAlarm.addAlarmAction(
        new cw_actions.SnsAction(props.alarmTopic)
      );
    }

    // Allow ALB to reach Fargate tasks
    this.service.connections.allowFrom(
      this.alb,
      ec2.Port.tcp(8000),
      "ALB to Fargate"
    );

    // Allow backend to reach game server
    if (props.gameServerSg) {
      this.service.connections.allowTo(
        props.gameServerSg,
        ec2.Port.tcp(8080),
        "Backend to GameServer"
      );
    }

    // Existing canonical resources remain unmanaged pending reviewed adoption.
    // Never delete and recreate working public records as part of this stack.

    // Outputs
    new cdk.CfnOutput(this, "AlbDnsName", {
      value: this.alb.loadBalancerDnsName,
    });
    new cdk.CfnOutput(this, "ClusterName", {
      value: cluster.clusterName,
    });
    new cdk.CfnOutput(this, "ServiceName", {
      value: this.service.serviceName,
    });
  }
}
