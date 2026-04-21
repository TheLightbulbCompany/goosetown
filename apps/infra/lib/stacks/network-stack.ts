import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as sns from "aws-cdk-lib/aws-sns";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cw_actions from "aws-cdk-lib/aws-cloudwatch-actions";
import { Construct } from "constructs";

export interface NetworkStackProps extends cdk.StackProps {
  environment: string;
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
      natGateways: 1,
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

    // NAT port-exhaustion alarm. Single NAT across 2 AZs is a known tradeoff
    // (see RUNBOOK.md and issue #165). This is awareness-only — on a real
    // AZ failure operators should see it and decide whether to deploy a
    // second NAT. Port allocation errors signal traffic bursts, not AZ loss,
    // so `breaching` would be too alarmist.
    if (props.alarmTopic) {
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
