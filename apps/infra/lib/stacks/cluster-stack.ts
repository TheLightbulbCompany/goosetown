import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";

export interface ClusterStackProps extends cdk.StackProps {
  environment: string;
  vpc: ec2.IVpc;
}

/**
 * Single shared ECS cluster per env. Both the backend service (ComputeStack)
 * and the game server service (GameServerStack) attach to this cluster — no
 * more split footprint.
 *
 * Container Insights is disabled to avoid per-task metric cost; service-level
 * metrics still come through CloudWatch via the Fargate platform.
 */
export class ClusterStack extends cdk.Stack {
  public readonly cluster: ecs.Cluster;
  /** Literal cluster name — safe to compare in tests; `cluster.clusterName` is a CFN token. */
  public readonly clusterLogicalName: string;

  constructor(scope: Construct, id: string, props: ClusterStackProps) {
    super(scope, id, props);

    this.clusterLogicalName = `goosetown-${props.environment}`;

    this.cluster = new ecs.Cluster(this, "Cluster", {
      vpc: props.vpc,
      clusterName: this.clusterLogicalName,
      // containerInsights (bool) is deprecated in favor of containerInsightsV2
      containerInsightsV2: ecs.ContainerInsights.DISABLED,
    });
  }
}
