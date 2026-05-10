import * as cdk from "aws-cdk-lib";
import * as events from "aws-cdk-lib/aws-events";
import * as sqs from "aws-cdk-lib/aws-sqs";
import { Construct } from "constructs";

export interface EventingStackProps extends cdk.StackProps {
  environment: string;
}

/**
 * EventBridge world bus + DLQ for GooseTown WorldGM narrative events.
 *
 * Architecture:
 *  - WorldGM publishes events to the bus (source: goosetown.worldgm)
 *  - Backend publishes game-server events to the bus (source: goosetown.backend)
 *  - Per-agent SQS queues subscribe via dynamically created EventBridge rules
 *    (created/deleted in application code on agent register/leave — NOT here)
 *  - A 14-day archive allows replaying events for debugging / recovery
 *  - The DLQ captures messages that fail delivery to per-agent queues
 *
 * Per-agent SQS queues are created/deleted via SDK on agent register/leave,
 * not in CDK — dynamic agents make static CDK definitions impractical.
 */
export class EventingStack extends cdk.Stack {
  public readonly bus: events.EventBus;
  public readonly dlq: sqs.Queue;

  constructor(scope: Construct, id: string, props: EventingStackProps) {
    super(scope, id, props);

    const env = props.environment;

    // World event bus: backend + worldgm publish here; per-agent SQS queues
    // subscribe via dynamically created rules (one per agent, routing on
    // DetailType: personal.<agentId>.*).
    this.bus = new events.EventBus(this, "WorldBus", {
      eventBusName: `goosetown-${env}-world`,
    });

    // 14-day archive for replay (debugging, recovery, feed backfill for Plan 4)
    new events.Archive(this, "WorldBusArchive", {
      sourceEventBus: this.bus,
      archiveName: `goosetown-${env}-world-archive`,
      eventPattern: { source: events.Match.prefix("goosetown.") },
      retention: cdk.Duration.days(14),
    });

    // Dead-letter queue captures failed deliveries from per-agent SQS targets.
    // 14-day retention gives operators time to replay or discard stuck messages.
    this.dlq = new sqs.Queue(this, "WorldDLQ", {
      queueName: `goosetown-${env}-world-dlq`,
      retentionPeriod: cdk.Duration.days(14),
    });

    new cdk.CfnOutput(this, "WorldBusArn", {
      value: this.bus.eventBusArn,
      exportName: `goosetown-${env}-world-bus-arn`,
    });
    new cdk.CfnOutput(this, "WorldBusName", {
      value: this.bus.eventBusName,
      exportName: `goosetown-${env}-world-bus-name`,
    });
    new cdk.CfnOutput(this, "WorldDLQArn", {
      value: this.dlq.queueArn,
      exportName: `goosetown-${env}-world-dlq-arn`,
    });
  }
}
