import * as cdk from "aws-cdk-lib";
import * as apigatewayv2 from "aws-cdk-lib/aws-apigatewayv2";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as ecr from "aws-cdk-lib/aws-ecr";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as route53Targets from "aws-cdk-lib/aws-route53-targets";
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
  wsApiId?: string;
}

export class ComputeStack extends cdk.Stack {
  public readonly alb: elbv2.ApplicationLoadBalancer;
  public readonly service: ecs.FargateService;
  public readonly repository: ecr.Repository;

  constructor(scope: Construct, id: string, props: ComputeStackProps) {
    super(scope, id, props);

    const env = props.environment;

    // ECR repository
    this.repository = new ecr.Repository(this, "Repository", {
      repositoryName: `goosetown-${env}-backend`,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [
        {
          maxImageCount: 10,
          description: "Keep last 10 images",
        },
      ],
    });

    // ECS Cluster
    const cluster = new ecs.Cluster(this, "Cluster", {
      clusterName: `goosetown-${env}`,
      vpc: props.vpc,
      containerInsightsV2: ecs.ContainerInsights.ENHANCED,
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
      image: ecs.ContainerImage.fromEcrRepository(this.repository, "latest"),
      logging: ecs.LogDrivers.awsLogs({
        logGroup,
        streamPrefix: "backend",
      }),
      environment: {
        AWS_REGION: "us-east-1",
        ENVIRONMENT: env,
        DYNAMODB_TABLE_PREFIX: `goosetown-${env}`,
        SPRITE_BUCKET: props.spriteBucket.bucketName,
        CONNECTIONS_TABLE: props.tables.connections.tableName,
        WS_STAGE: env,
        ...(props.wsApiId ? { WS_API_ID: props.wsApiId } : {}),
        CORS_ORIGINS: env === "prod"
          ? "https://goosetown.isol8.co"
          : "https://dev.goosetown.isol8.co,http://localhost:3000,http://localhost:5173",
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
    this.service = new ecs.FargateService(this, "Service", {
      serviceName: `goosetown-${env}-backend`,
      cluster,
      taskDefinition,
      desiredCount: 1,
      assignPublicIp: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      circuitBreaker: { rollback: true },
      minHealthyPercent: 100,
    });

    this.service.attachToApplicationTargetGroup(targetGroup);

    // Allow ALB to reach Fargate tasks
    this.service.connections.allowFrom(
      this.alb,
      ec2.Port.tcp(8000),
      "ALB to Fargate"
    );

    // ─── NLB + VPC Link for WebSocket $default route ─────────────────
    // API Gateway WebSocket cannot use ALB directly — it requires an NLB
    // via VPC Link for HTTP_PROXY integrations into the VPC.
    if (props.wsApiId) {
      const nlb = new elbv2.NetworkLoadBalancer(this, "WsNlb", {
        loadBalancerName: `goosetown-${env}-ws-nlb`,
        vpc: props.vpc,
        internetFacing: false,
        vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      });

      const nlbTargetGroup = new elbv2.NetworkTargetGroup(this, "WsNlbTargetGroup", {
        targetGroupName: `goosetown-${env}-ws-nlb-tg`,
        vpc: props.vpc,
        port: 8000,
        protocol: elbv2.Protocol.TCP,
        targetType: elbv2.TargetType.IP,
        healthCheck: {
          protocol: elbv2.Protocol.HTTP,
          path: "/health",
          interval: cdk.Duration.seconds(30),
          healthyThresholdCount: 2,
          unhealthyThresholdCount: 3,
        },
      });

      nlb.addListener("WsNlbListener", {
        port: 8000,
        protocol: elbv2.Protocol.TCP,
        defaultTargetGroups: [nlbTargetGroup],
      });

      // Register Fargate tasks with the NLB target group
      this.service.attachToNetworkTargetGroup(nlbTargetGroup);

      // Allow NLB to reach Fargate tasks
      this.service.connections.allowFrom(
        ec2.Peer.ipv4(props.vpc.vpcCidrBlock),
        ec2.Port.tcp(8000),
        "NLB to Fargate"
      );

      // VPC Link for API Gateway to reach the NLB
      const vpcLink = new apigatewayv2.CfnVpcLink(this, "WsVpcLink", {
        name: `goosetown-${env}-ws-vpclink`,
        subnetIds: props.vpc.privateSubnets.map((s) => s.subnetId),
        securityGroupIds: [],
      });

      // $default route integration — forwards WS messages to Fargate
      const defaultIntegration = new apigatewayv2.CfnIntegration(
        this,
        "WsDefaultIntegration",
        {
          apiId: props.wsApiId,
          integrationType: "HTTP_PROXY",
          integrationMethod: "POST",
          integrationUri: `http://${nlb.loadBalancerDnsName}:8000/api/v1/town/ws/message`,
          connectionType: "VPC_LINK",
          connectionId: vpcLink.ref,
          requestParameters: {
            "integration.request.header.connectionId":
              "context.connectionId",
          },
        }
      );

      const defaultRoute = new apigatewayv2.CfnRoute(this, "WsDefaultRoute", {
        apiId: props.wsApiId,
        routeKey: "$default",
        target: `integrations/${defaultIntegration.ref}`,
      });
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
    new cdk.CfnOutput(this, "EcrRepositoryUri", {
      value: this.repository.repositoryUri,
    });
    new cdk.CfnOutput(this, "ClusterName", {
      value: cluster.clusterName,
    });
    new cdk.CfnOutput(this, "ServiceName", {
      value: this.service.serviceName,
    });
  }
}
