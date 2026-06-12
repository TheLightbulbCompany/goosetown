import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as iam from "aws-cdk-lib/aws-iam";
import * as logs from "aws-cdk-lib/aws-logs";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as cloudwatch from "aws-cdk-lib/aws-cloudwatch";
import * as cw_actions from "aws-cdk-lib/aws-cloudwatch-actions";
import * as sns from "aws-cdk-lib/aws-sns";
import { DockerImageAsset, Platform } from "aws-cdk-lib/aws-ecr-assets";
import * as path from "path";
import { Construct } from "constructs";
import { AuthSecrets } from "./auth-stack";
import { DatabaseTables } from "./database-stack";

export interface SpriteWorkerStackProps extends cdk.StackProps {
  environment: string;
  cluster: ecs.ICluster;
  vpc: ec2.IVpc;
  secrets: AuthSecrets;
  tables: DatabaseTables;
  spriteBucket: s3.IBucket;
  alarmTopic?: sns.ITopic;
}

/**
 * Sprite worker — single-replica Fargate service that drains the durable
 * sprite-jobs SQS queue and is the ONLY process that calls PixelLab
 * generation, making the per-process animation semaphore a true global
 * concurrency cap (spec 2026-06-11). Same packaging pattern as WorldgmStack:
 * backend Docker image, command override.
 *
 * Recovery model: a worker death surfaces in-flight messages within the
 * 300s visibility timeout; 3 failed receives dead-letter the job (alarmed).
 * Jobs survive worker AND backend deploys — that is the point.
 */
export class SpriteWorkerStack extends cdk.Stack {
  public readonly service: ecs.FargateService;
  public readonly queue: sqs.Queue;

  constructor(scope: Construct, id: string, props: SpriteWorkerStackProps) {
    super(scope, id, props);
    const env = props.environment;

    const dlq = new sqs.Queue(this, "SpriteJobsDlq", {
      queueName: `goosetown-${env}-sprite-jobs-dlq`,
      retentionPeriod: cdk.Duration.days(14),
    });

    this.queue = new sqs.Queue(this, "SpriteJobsQueue", {
      // Physical name is load-bearing: producers (backend) and the worker
      // resolve it at runtime via GetQueueUrl by name — no cross-stack object.
      queueName: `goosetown-${env}-sprite-jobs`,
      // 300s + the worker's 120s ChangeMessageVisibility heartbeat: a long
      // pipeline never re-surfaces mid-run, a dead worker's message returns
      // within <=5 min.
      visibilityTimeout: cdk.Duration.seconds(300),
      retentionPeriod: cdk.Duration.days(4),
      receiveMessageWaitTime: cdk.Duration.seconds(20),
      deadLetterQueue: { queue: dlq, maxReceiveCount: 3 },
    });

    // Reuse the backend Docker image — CDK dedupes builds by directory hash.
    const backendImage = new DockerImageAsset(this, "SpriteWorkerImage", {
      directory: path.join(__dirname, "..", "..", "..", "backend"),
      platform: Platform.LINUX_AMD64,
    });

    const logGroup = new logs.LogGroup(this, "LogGroup", {
      logGroupName: `/ecs/goosetown-${env}-sprite-worker`,
      retention: logs.RetentionDays.TWO_WEEKS,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const taskDef = new ecs.FargateTaskDefinition(this, "TaskDef", {
      // 0.5 vCPU / 1 GB: PixelLab polling is IO-bound; PIL compositing bursts
      // are the only CPU users and they're per-agent serialized.
      cpu: 512,
      memoryLimitMiB: 1024,
    });

    taskDef.addContainer("SpriteWorker", {
      image: ecs.ContainerImage.fromDockerImageAsset(backendImage),
      command: ["uv", "run", "python", "-m", "sprite_worker"],
      logging: ecs.LogDrivers.awsLogs({ logGroup, streamPrefix: "sprite-worker" }),
      environment: {
        AWS_REGION: cdk.Stack.of(this).region,
        ENVIRONMENT: env,
        DYNAMODB_TABLE_PREFIX: `goosetown-${env}`,
        SPRITE_S3_BUCKET: props.spriteBucket.bucketName,
        SPRITE_CDN_URL: `https://assets-${env}.goosetown.isol8.co`,
        SPRITE_JOBS_QUEUE_NAME: `goosetown-${env}-sprite-jobs`,
        // Inline custom-domain string, NOT an Fn.importValue — same rationale
        // as compute-stack's WS_MANAGEMENT_API_URL (avoids cross-stack cycles).
        BACKEND_INTERNAL_URL: `https://api-${env}.goosetown.isol8.co`,
        // Global PixelLab animation concurrency (each animation = up to 4
        // jobs). Tune after PixelLab confirms the account cap.
        PIXELLAB_MAX_CONCURRENT_ANIMATIONS: "2",
        SPRITE_WORKER_MAX_CONCURRENT_PIPELINES: "2",
      },
      secrets: {
        PIXELLAB_API_KEY: ecs.Secret.fromSecretsManager(props.secrets.pixelLabApiKey),
      },
    });

    this.queue.grantConsumeMessages(taskDef.taskRole);
    this.queue.grantSendMessages(taskDef.taskRole); // boot-sweep re-enqueue
    props.tables.agents.grantReadWriteData(taskDef.taskRole);
    props.spriteBucket.grantReadWrite(taskDef.taskRole);
    // PutMetricData has no resource-level scoping; namespace is by convention
    // (GooseTown/SpriteWorker) — same note as WorldgmStack.
    taskDef.taskRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        actions: ["cloudwatch:PutMetricData"],
        resources: ["*"],
      }),
    );

    this.service = new ecs.FargateService(this, "Service", {
      serviceName: `goosetown-${env}-sprite-worker`,
      cluster: props.cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      // Stop-then-start on deploys: two concurrent workers would each run a
      // boot sweep + their own "global" semaphore, double-generating against
      // PixelLab during the overlap. A ~1 min generation gap is fine — jobs
      // are durable in SQS.
      minHealthyPercent: 0,
      maxHealthyPercent: 100,
      assignPublicIp: false,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
      circuitBreaker: { rollback: true },
    });

    if (props.alarmTopic) {
      const dlqAlarm = new cloudwatch.Alarm(this, "SpriteJobsDlqNotEmpty", {
        alarmName: `goosetown-${env}-sprite-jobs-dlq-not-empty`,
        metric: dlq.metricApproximateNumberOfMessagesVisible({ period: cdk.Duration.minutes(5) }),
        threshold: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        alarmDescription:
          "Sprite jobs are dead-lettering (3 genuine processing failures each). Credit-paused jobs never reach the DLQ — they re-enqueue with a fresh receive count. Check sprite-worker logs for the root cause. See RUNBOOK.md.",
      });
      dlqAlarm.addAlarmAction(new cw_actions.SnsAction(props.alarmTopic));

      const ageAlarm = new cloudwatch.Alarm(this, "SpriteJobsBacklogAge", {
        alarmName: `goosetown-${env}-sprite-jobs-backlog-age`,
        metric: this.queue.metricApproximateAgeOfOldestMessage({ period: cdk.Duration.minutes(5) }),
        threshold: 1800,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        evaluationPeriods: 3,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        alarmDescription: "Oldest sprite job > 30 min — worker stuck, dead, or PixelLab degraded.",
      });
      ageAlarm.addAlarmAction(new cw_actions.SnsAction(props.alarmTopic));

      const taskMissingAlarm = new cloudwatch.Alarm(this, "SpriteWorkerTaskMissing", {
        alarmName: `goosetown-${env}-sprite-worker-task-missing`,
        metric: new cloudwatch.Metric({
          namespace: "AWS/ECS",
          metricName: "CPUUtilization",
          dimensionsMap: {
            ClusterName: props.cluster.clusterName,
            ServiceName: this.service.serviceName,
          },
          statistic: "SampleCount",
          period: cdk.Duration.minutes(1),
        }),
        threshold: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
        evaluationPeriods: 5,
        treatMissingData: cloudwatch.TreatMissingData.BREACHING,
        alarmDescription:
          "Sprite worker has no running task. New signups keep their static placeholder until it returns (jobs are durable in SQS).",
      });
      taskMissingAlarm.addAlarmAction(new cw_actions.SnsAction(props.alarmTopic));

      const creditAlarm = new cloudwatch.Alarm(this, "PixelLabCreditExhausted", {
        alarmName: `goosetown-${env}-pixellab-credit-exhausted`,
        metric: new cloudwatch.Metric({
          namespace: "GooseTown/SpriteWorker",
          metricName: "CreditExhausted",
          statistic: "Sum",
          period: cdk.Duration.minutes(5),
        }),
        threshold: 1,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
        evaluationPeriods: 1,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
        alarmDescription:
          "PixelLab returned 402 (out of generations). Top up credits; queued jobs auto-resume. Closes the credit-exhaustion launch blocker.",
      });
      creditAlarm.addAlarmAction(new cw_actions.SnsAction(props.alarmTopic));
    }

    new cdk.CfnOutput(this, "QueueName", { value: this.queue.queueName });
    new cdk.CfnOutput(this, "ServiceName", { value: this.service.serviceName });
  }
}
