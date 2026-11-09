import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as sns from "aws-cdk-lib/aws-sns";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cw_actions from "aws-cdk-lib/aws-cloudwatch-actions";
import { Construct } from "constructs";

export interface NetworkStackProps extends cdk.StackProps {
  environment: string;
  /** Legacy world is parked; a deployment must not restart its workers. */
  paused?: boolean;
  /** SNS topic for CloudWatch alarm actions. Wired by ObservabilityStack. */
  alarmTopic?: sns.ITopic;
}

export class NetworkStack extends cdk.Stack {
  public readonly vpc: ec2.Vpc;

  constructor(scope: Construct, id: string, props: NetworkStackProps) {
    super(scope, id, props);

    this.vpc = new ec2.Vpc(this, "Vpc", {
      vpcName: `goosetown-${props.environment}-vpc`,
      maxAzs: 2,
      // Single NAT in every env. This is a deliberate cost-over-HA call:
      // a NAT-AZ outage takes down private-subnet egress until AWS recovers
      // the AZ, and we accept that in exchange for ~$32/mo per extra NAT.
      natGateways: props.paused ? 0 : 1,
      subnetConfiguration: [
        {
          cidrMask: 24,
          name: "public",
          subnetType: ec2.SubnetType.PUBLIC,
        },
        {
          cidrMask: 24,
          name: "private",
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
        },
      ],
    });

    new cdk.CfnOutput(this, "VpcId", {
      value: this.vpc.vpcId,
      exportName: `goosetown-${props.environment}-vpc-id`,
    });

    // ── Free Gateway VPC endpoints (DynamoDB + S3) ─────────────────────────
    // Gateway endpoints are FREE — no hourly charge, no per-GB transfer fee.
    // Traffic from Fargate tasks to DynamoDB and S3 is routed through the
    // endpoint rather than through the NAT Gateway, saving NAT data-processing
    // costs as agent traffic ramps up.
    //
    // Interface endpoints (e.g. Secrets Manager, ECR API) cost ~$22/mo per AZ
    // and are only worth it at higher volume; they are deferred until we have
    // concrete NAT data-cost signal. See Known Issues for the single-NAT-AZ
    // risk note — that is the higher priority egress HA item.
    this.vpc.addGatewayEndpoint("S3Endpoint", {
      service: ec2.GatewayVpcEndpointAwsService.S3,
    });
    this.vpc.addGatewayEndpoint("DynamoDbEndpoint", {
      service: ec2.GatewayVpcEndpointAwsService.DYNAMODB,
    });

    // NAT port-exhaustion alarm. Port allocation errors signal traffic bursts
    // rather than AZ loss, so `notBreaching` on missing data avoids false
    // alarms during quiet periods.
    if (props.alarmTopic && !props.paused) {
      const natErrorAlarm = new cloudwatch.Alarm(this, "NatPortErrorAlarm", {
        alarmName: `goosetown-${props.environment}-nat-port-exhaustion`,
        metric: new cloudwatch.Metric({
          namespace: "AWS/NATGateway",
          metricName: "ErrorPortAllocation",
          statistic: "Sum",
          period: cdk.Duration.minutes(1),
        }),
        threshold: 1,
        comparisonOperator:
          cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        evaluationPeriods: 5,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        alarmDescription:
          "NAT gateway is dropping connections due to port exhaustion. See RUNBOOK.md.",
      });
      natErrorAlarm.addAlarmAction(new cw_actions.SnsAction(props.alarmTopic));
    }
  }
}
