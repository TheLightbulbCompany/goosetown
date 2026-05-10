import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cw_actions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as lambda_ from "aws-cdk-lib/aws-lambda";
import * as logs from "aws-cdk-lib/aws-logs";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as sns from "aws-cdk-lib/aws-sns";
import { Construct } from "constructs";

export interface ObservabilityStackProps extends cdk.StackProps {
  environment: string;
  /** Threshold (raw credit count) below which the alarm fires.
   *  Default 1000 credits remaining → ~50 future signups at ~20
   *  credits per character. Tunable per env. */
  pixelLabCreditAlarmThreshold?: number;
}

export class ObservabilityStack extends cdk.Stack {
  public readonly snsTopic: sns.Topic;

  constructor(scope: Construct, id: string, props: ObservabilityStackProps) {
    super(scope, id, props);

    const env = props.environment;

    // SNS topic for all GooseTown alarms in this environment.
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

    // ─── PixelLab credit-balance poller ─────────────────────────────────
    // PixelLab is the largest unbounded cost exposure in the system: a
    // 100-signup spike drains credits silently and sprite generation
    // starts failing for everyone. PixelLab does not push to CloudWatch
    // natively, so this Lambda polls their balance endpoint every 15
    // minutes and emits a custom CloudWatch metric. The alarm below
    // pages on-call when remaining credits drop below the threshold,
    // giving humans time to top up before agents start failing to
    // register. Producer's 2026-05-04 launch-readiness plan, Tier 1 #1.1.

    // Look up the PixelLab secret by name rather than taking it as a stack
    // prop — avoids a circular dependency with AuthStack (which creates the
    // secret) and goosetown-stage's deploy ordering (Observability deploys
    // first because the SNS topic is consumed by Network/GameServer/Compute).
    // The secret name is deterministic; AuthStack creates it as
    // ``goosetown/${env}/pixellab_api_key`` (slash-and-underscore form, not
    // hyphenated — matches the convention used by all five auth secrets, see
    // ``auth-stack.ts``). Prior to 2026-05-07 this lookup used the hyphenated
    // name, which never existed in Secrets Manager — every poller invocation
    // failed with ``ResourceNotFoundException`` and the credit metric was
    // missing rather than published, so the alarm latched on missing data
    // and reported ``ALARM`` regardless of the real PixelLab balance.
    const pixelLabApiKey = secretsmanager.Secret.fromSecretNameV2(
      this,
      "PixelLabApiKey",
      `goosetown/${env}/pixellab_api_key`,
    );

    const pollerLogGroup = new logs.LogGroup(this, "PixelLabCreditsPollerLogs", {
      logGroupName: `/aws/lambda/goosetown-${env}-pixellab-credits-poller`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const pollerFn = new lambda_.Function(this, "PixelLabCreditsPoller", {
      functionName: `goosetown-${env}-pixellab-credits-poller`,
      runtime: lambda_.Runtime.PYTHON_3_12,
      handler: "index.handler",
      timeout: cdk.Duration.seconds(30),
      memorySize: 128,
      logGroup: pollerLogGroup,
      environment: {
        ENVIRONMENT: env,
        PIXELLAB_API_KEY_SECRET_NAME: pixelLabApiKey.secretName,
        // PixelLab's /v2/balance response shape changed — it now returns:
        //   { credits: { type: "usd", usd: 0.0 },
        //     subscription: { type: "generations", generations: 4766.0,
        //                     total: 5000.0 } }
        // The handler's default ``credits_remaining`` path no longer exists.
        // For GooseTown's monthly subscription model, the meaningful capacity
        // is ``subscription.generations`` (remaining out of ``total``) — that's
        // what gates sprite generation. Override here so the poller publishes
        // the right number; alarm threshold (1000) is interpreted against this.
        PIXELLAB_BALANCE_JSON_PATH: "subscription.generations",
      },
      code: lambda_.Code.fromAsset(
        path.join(__dirname, "..", "..", "lambda", "pixellab-credits-poller"),
      ),
    });

    // Grant secret read.
    pixelLabApiKey.grantRead(pollerFn);

    // Grant CloudWatch metric write. Cross-namespace PutMetricData has no
    // resource-scoping in IAM (CloudWatch enforces by namespace dimension
    // at metric-publication time), so we grant the action with `*`.
    pollerFn.addToRolePolicy(
      new cdk.aws_iam.PolicyStatement({
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
      }),
    );

    // EventBridge schedule: every 15 minutes.
    const schedule = new events.Rule(this, "PixelLabCreditsPollerSchedule", {
      ruleName: `goosetown-${env}-pixellab-credits-poll`,
      schedule: events.Schedule.rate(cdk.Duration.minutes(15)),
      description:
        "Polls PixelLab balance every 15 minutes; emits CloudWatch metric.",
    });
    schedule.addTarget(new targets.LambdaFunction(pollerFn));

    // ─── Alarm: PixelLab credits depleted ────────────────────────────────
    const creditAlarmThreshold = props.pixelLabCreditAlarmThreshold ?? 1000;

    const creditAlarm = new cloudwatch.Alarm(this, "PixelLabCreditsLowAlarm", {
      alarmName: `goosetown-${env}-pixellab-credits-low`,
      alarmDescription: `PixelLab credit balance dropped below ${creditAlarmThreshold}. New agent registrations will start failing once credits hit zero. Top up at https://pixellab.ai. Runbook: this is a hard stop — no automated mitigation; an operator must add credits.`,
      metric: new cloudwatch.Metric({
        namespace: "GooseTown",
        metricName: "PixelLabCreditsRemaining",
        dimensionsMap: { Environment: env },
        statistic: "Minimum",
        // 15-minute period matches the poller's cadence; one missed
        // datapoint is the smallest unit of resolution.
        period: cdk.Duration.minutes(15),
      }),
      threshold: creditAlarmThreshold,
      comparisonOperator:
        cloudwatch.ComparisonOperator.LESS_THAN_OR_EQUAL_TO_THRESHOLD,
      // 2 of 3 evaluation periods to require sustained low balance —
      // avoids paging on a single transient PixelLab API blip.
      evaluationPeriods: 3,
      datapointsToAlarm: 2,
      // If the poller fails entirely (PixelLab outage / credential issue),
      // the handler emits a 0-credits datapoint, so missing-data is treated
      // as ALARM defensively. We'd rather wake on-call for a poller bug
      // than miss a real drain.
      treatMissingData: cloudwatch.TreatMissingData.BREACHING,
    });
    creditAlarm.addAlarmAction(new cw_actions.SnsAction(this.snsTopic));
  }
}
