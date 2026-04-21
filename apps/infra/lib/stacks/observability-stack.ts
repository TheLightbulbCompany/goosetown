import * as cdk from "aws-cdk-lib";
import * as sns from "aws-cdk-lib/aws-sns";
import { Construct } from "constructs";

export interface ObservabilityStackProps extends cdk.StackProps {
  environment: string;
}

export class ObservabilityStack extends cdk.Stack {
  public readonly snsTopic: sns.Topic;

  constructor(scope: Construct, id: string, props: ObservabilityStackProps) {
    super(scope, id, props);

    const env = props.environment;

    // TODO: subscribe operator email via SNS console or CDK context
    // e.g.: aws sns subscribe --topic-arn <arn> --protocol email --notification-endpoint <email>
    this.snsTopic = new sns.Topic(this, "AlarmTopic", {
      topicName: `goosetown-${env}-alarms`,
      displayName: `GooseTown ${env} alarms`,
    });

    new cdk.CfnOutput(this, "AlarmTopicArn", {
      value: this.snsTopic.topicArn,
      exportName: `goosetown-${env}-alarm-topic-arn`,
    });
  }
}
