import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import { DockerImageAsset, Platform } from "aws-cdk-lib/aws-ecr-assets";
import * as logs from "aws-cdk-lib/aws-logs";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import * as path from "path";
import { Construct } from "constructs";

export interface GameServerStackProps extends cdk.StackProps {
  environment: string;
  vpc: ec2.IVpc;
}

export class GameServerStack extends cdk.Stack {
  public readonly serviceUrl: string;
  public readonly securityGroup: ec2.ISecurityGroup;

  constructor(scope: Construct, id: string, props: GameServerStackProps) {
    super(scope, id, props);
    const env = props.environment;

    // Docker image from apps/godot/
    const dockerImage = new DockerImageAsset(this, "GameServerImage", {
      directory: path.join(__dirname, "..", "..", "..", "godot"),
      platform: Platform.LINUX_AMD64,
    });

    // Cloud Map namespace for service discovery
    const namespace = new servicediscovery.PrivateDnsNamespace(
      this,
      "Namespace",
      {
        name: `goosetown-${env}.local`,
        vpc: props.vpc,
      }
    );

    // ECS cluster (separate from backend cluster)
    const cluster = new ecs.Cluster(this, "Cluster", {
      vpc: props.vpc,
    });

    // Log group
    const logGroup = new logs.LogGroup(this, "LogGroup", {
      logGroupName: `/ecs/goosetown-${env}-gameserver`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Task definition - 512 CPU, 1GB RAM
    const taskDef = new ecs.FargateTaskDefinition(this, "TaskDef", {
      cpu: 512,
      memoryLimitMiB: 1024,
    });

    taskDef.addContainer("GameServer", {
      image: ecs.ContainerImage.fromDockerImageAsset(dockerImage),
      portMappings: [{ containerPort: 8080, protocol: ecs.Protocol.TCP }],
      logging: ecs.LogDrivers.awsLogs({
        logGroup,
        streamPrefix: "gameserver",
      }),
      healthCheck: {
        command: [
          "CMD-SHELL",
          "bash -c 'echo > /dev/tcp/localhost/8080' || exit 1",
        ],
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        retries: 5,
        startPeriod: cdk.Duration.seconds(120),
      },
    });

    // Fargate service with Cloud Map
    const service = new ecs.FargateService(this, "Service", {
      cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      assignPublicIp: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      circuitBreaker: { rollback: true },
      cloudMapOptions: {
        name: "gameserver",
        cloudMapNamespace: namespace,
        dnsRecordType: servicediscovery.DnsRecordType.A,
      },
    });

    this.securityGroup = service.connections.securityGroups[0];
    this.serviceUrl = `gameserver.goosetown-${env}.local`;

    // Outputs
    new cdk.CfnOutput(this, "ServiceUrl", { value: this.serviceUrl });
    new cdk.CfnOutput(this, "ClusterName", {
      value: cluster.clusterName,
    });
    new cdk.CfnOutput(this, "ServiceName", {
      value: service.serviceName,
    });
  }
}
