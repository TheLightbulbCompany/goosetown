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
import { Construct } from "constructs";
import { DatabaseTables } from "./database-stack";
import { AuthSecrets } from "./auth-stack";

export interface ComputeStackProps extends cdk.StackProps {
  environment: string;
  vpc: ec2.IVpc;
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

    // ECS Cluster — Container Insights disabled to avoid per-task metric
    // explosion.  Enhanced Container Insights publishes dozens of metrics
    // per task, and we only run a single backend service here.  AWS/ECS
    // standard metrics (CPUUtilization, MemoryUtilization per service) are
    // always published and cover our alerting needs.
    const cluster = new ecs.Cluster(this, "Cluster", {
      clusterName: `goosetown-${env}`,
      vpc: props.vpc,
      containerInsightsV2: ecs.ContainerInsights.DISABLED,
    });

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
    Object.values(props.secrets).forEach((secret) => {
      secret.grantRead(executionRole);
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
    Object.values(props.secrets).forEach((secret) => {
      secret.grantRead(taskRole);
    });

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
        cpu: 512,
        memoryLimitMiB: 1024,
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
        DYNAMODB_TABLE_PREFIX: `goosetown-${env}`,
        SPRITE_S3_BUCKET: props.spriteBucket.bucketName,
        SPRITE_CDN_URL: `https://assets-${env}.goosetown.isol8.co`,
        CORS_ORIGINS: env === "prod"
          ? "https://goosetown.isol8.co"
          : "https://dev.goosetown.isol8.co,http://localhost:3000,http://localhost:5173",
        ...(props.gameServerUrl && {
          GAME_SERVER_URL: `ws://${props.gameServerUrl}:8080`,
        }),
        TOWN_WS_URL: env === "prod"
          ? "wss://ws.goosetown.isol8.co"
          : "wss://ws-dev.goosetown.isol8.co",
        TOWN_API_URL: env === "prod"
          ? "https://api.goosetown.isol8.co/api/v1"
          : "https://api-dev.goosetown.isol8.co/api/v1",
        // Using the custom-domain string avoids an Fn.importValue cross-stack
        // reference from ComputeStack to ApiStack — which would be circular
        // because ApiStack already depends on ComputeStack via fargateTaskRole,
        // and Compute deploys before Api on fresh environments.
        // The ApiStack export (goosetown-${env}-management-api-url) is kept in
        // place for at least one week of clean prod runtime before removal.
        WS_MANAGEMENT_API_URL: env === "prod"
          ? "https://ws.goosetown.isol8.co"
          : "https://ws-dev.goosetown.isol8.co",
      },
      secrets: {
        CLERK_ISSUER: ecs.Secret.fromSecretsManager(props.secrets.clerkIssuer),
        CLERK_SECRET_KEY: ecs.Secret.fromSecretsManager(
          props.secrets.clerkSecretKey
        ),
        TOWN_TOKEN_SECRET: ecs.Secret.fromSecretsManager(
          props.secrets.townTokenSecret
        ),
        PIXELLAB_API_KEY: ecs.Secret.fromSecretsManager(
          props.secrets.pixelLabApiKey
        ),
      },
      portMappings: [{ containerPort: 8000, protocol: ecs.Protocol.TCP }],
      healthCheck: {
        command: [
          "CMD-SHELL",
          "curl -f http://localhost:8000/health || exit 1",
        ],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
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
      removalPolicy: cdk.RemovalPolicy.RETAIN,
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
        timeout: cdk.Duration.seconds(5),
        healthyThresholdCount: 2,
        unhealthyThresholdCount: 3,
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
    // Prod: 2 tasks + minHealthyPercent:100 → true HA, zero-downtime rolling deploys.
    // Dev:  1 task + minHealthyPercent:0   → accepts ~30s downtime, unblocks rolling
    //       update so a single unhealthy task doesn't crash-loop indefinitely.
    const isProd = env === "prod";
    this.service = new ecs.FargateService(this, "Service", {
      serviceName: `goosetown-${env}-backend`,
      cluster,
      taskDefinition,
      desiredCount: isProd ? 2 : 1,
      assignPublicIp: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      circuitBreaker: { rollback: true },
      minHealthyPercent: isProd ? 100 : 0,
    });

    this.service.attachToApplicationTargetGroup(targetGroup);

    // Auto-scaling on CPU. Prod gets 2-6 tasks (2 baseline for HA, 6 ceiling
    // protects budget on a runaway scrape); dev gets 1-3 (1 baseline so a
    // bad health check can recover via rolling update, 3 ceiling so a
    // misconfigured load test in dev can't accidentally autoscale to prod
    // levels). Target 60% CPU — leaves headroom for sprite-pipeline bursts
    // before sustained breach forces scale-out. Game server stays single-
    // replica until sharding is designed (see gameserver-stack.ts).
    const scaling = this.service.autoScaleTaskCount({
      minCapacity: isProd ? 2 : 1,
      maxCapacity: isProd ? 6 : 3,
    });
    scaling.scaleOnCpuUtilization("CpuScaling", {
      targetUtilizationPercent: 60,
      scaleInCooldown: cdk.Duration.seconds(60),
      scaleOutCooldown: cdk.Duration.seconds(60),
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
        threshold: isProd ? 2 : 1,
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

    // DNS record: api-{env}.goosetown.isol8.co -> ALB
    if (props.hostedZone) {
      new route53.ARecord(this, "ApiDnsRecord", {
        zone: props.hostedZone,
        recordName: `api-${env}.goosetown.isol8.co`,
        target: route53.RecordTarget.fromAlias(
          new route53Targets.LoadBalancerTarget(this.alb)
        ),
      });
    }

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
