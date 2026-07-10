import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import { DockerImageAsset, Platform } from "aws-cdk-lib/aws-ecr-assets";
import * as logs from "aws-cdk-lib/aws-logs";
import * as servicediscovery from "aws-cdk-lib/aws-servicediscovery";
import * as sns from "aws-cdk-lib/aws-sns";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cw_actions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as path from "path";
import { Construct } from "constructs";

export interface GameServerStackProps extends cdk.StackProps {
  environment: string;
  vpc: ec2.IVpc;
  /** Shared ECS cluster from ClusterStack — both services run on the same cluster. */
  cluster: ecs.ICluster;
  /** SNS topic for CloudWatch alarm actions. Wired by ObservabilityStack. */
  alarmTopic?: sns.ITopic;
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

    // ECS Cluster — shared with ComputeStack via ClusterStack cross-stack
    // reference. Container Insights is disabled in ClusterStack (cost reason);
    // the game-server alarm now uses HealthyHostCount instead of
    // RunningTaskCount (which requires Container Insights).
    const cluster = props.cluster;

    // Log group
    const logGroup = new logs.LogGroup(this, "LogGroup", {
      logGroupName: `/ecs/goosetown-${env}-gameserver`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Task definition - 256 CPU, 0.5GB RAM. Rightsized from 512/1024 per
    // Compute Optimizer (14-day p50/peak well under half a vCPU; the game
    // loop is IO-bound on the WS broadcast, not CPU).
    const taskDef = new ecs.FargateTaskDefinition(this, "TaskDef", {
      cpu: 256,
      memoryLimitMiB: 512,
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

    // Single-replica game server (sharding not yet designed). Alarm fires if
    // the service stops publishing CPU datapoints for 5 consecutive minutes —
    // at that point OpenClaw agents have exhausted their 5 reconnect attempts
    // and have dropped permanently. Operator response: restart per RUNBOOK.md.
    if (props.alarmTopic) {
      const taskMissingAlarm = new cloudwatch.Alarm(this, "GameServerTaskMissing", {
        alarmName: `goosetown-${env}-gameserver-task-missing`,
        // Container Insights is disabled on the shared cluster (cost — see
        // ClusterStack), so ECS/ContainerInsights:RunningTaskCount is never
        // published and any alarm on it latches BREACHING. Use AWS/ECS:
        // CPUUtilization instead — Fargate publishes it every minute when at
        // least one task is running; with statistic=SampleCount the metric
        // value is 1 (datapoint present) per period when tasks run, and the
        // datapoint is absent when zero tasks → TreatMissingData=BREACHING
        // fires the alarm. Same shape as ComputeStack's BackendTaskMissing
        // alarm (which uses ALB HealthyHostCount; gameserver has no ALB).
        metric: new cloudwatch.Metric({
          namespace: "AWS/ECS",
          metricName: "CPUUtilization",
          dimensionsMap: {
            ClusterName: cluster.clusterName,
            ServiceName: service.serviceName,
          },
          statistic: "SampleCount",
          period: cdk.Duration.minutes(1),
        }),
        threshold: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        evaluationPeriods: 5,
        treatMissingData: cloudwatch.TreatMissingData.BREACHING,
        alarmDescription:
          "Game server has no running task. Agents cannot connect. See RUNBOOK.md.",
      });
      taskMissingAlarm.addAlarmAction(new cw_actions.SnsAction(props.alarmTopic));
    }

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
