import { canonicalHost } from "./stacks/canonical-domain";
import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import { NetworkStack } from "./stacks/network-stack";
import { DatabaseStack } from "./stacks/database-stack";
import { AuthStack } from "./stacks/auth-stack";
import { DnsStack } from "./stacks/dns-stack";
import { StorageStack } from "./stacks/storage-stack";
import { ClusterStack } from "./stacks/cluster-stack";
import { ComputeStack } from "./stacks/compute-stack";
import { GameServerStack } from "./stacks/gameserver-stack";
import { ApiStack } from "./stacks/api-stack";
import { ObservabilityStack } from "./stacks/observability-stack";
import { EventingStack } from "./stacks/eventing-stack";
import { WorldgmStack } from "./stacks/worldgm-stack";
import { SpriteWorkerStack } from "./stacks/sprite-worker-stack";

export interface GooseTownStageProps extends cdk.StageProps {
  environment: string;
}

export class GooseTownStage extends cdk.Stage {
  constructor(scope: Construct, id: string, props: GooseTownStageProps) {
    super(scope, id, props);
    const env = props.environment;

    // IMPORTANT: Explicit stackName on every stack preserves existing CloudFormation
    // stack names when moving from app-scope to Stage-scope. Without this, CDK
    // prepends the stage ID, creating new stacks and orphaning existing ones.

    // ObservabilityStack deploys first — no Fargate/Lambda dependencies.
    // Creates the shared SNS alarm topic that other stacks publish to.
    const observability = new ObservabilityStack(
      this,
      `goosetown-${env}-observability`,
      {
        stackName: `goosetown-${env}-observability`,
        environment: env,
      }
    );

    const network = new NetworkStack(this, `goosetown-${env}-network`, {
      stackName: `goosetown-${env}-network`,
      environment: env,
      alarmTopic: observability.snsTopic,
    });
    const database = new DatabaseStack(this, `goosetown-${env}-database`, {
      stackName: `goosetown-${env}-database`,
      environment: env,
    });
    const auth = new AuthStack(this, `goosetown-${env}-auth`, {
      stackName: `goosetown-${env}-auth`,
      environment: env,
    });
    const dns = new DnsStack(this, `goosetown-${env}-dns`, {
      stackName: `goosetown-${env}-dns`,
      environment: env,
    });

    const storage = new StorageStack(this, `goosetown-${env}-storage`, {
      stackName: `goosetown-${env}-storage`,
      environment: env,
      certificate: dns.certificate,
      hostedZone: dns.hostedZone,
    });

    const clusterStack = new ClusterStack(this, `goosetown-${env}-cluster`, {
      stackName: `goosetown-${env}-cluster`,
      environment: env,
      vpc: network.vpc,
    });

    const gameServer = new GameServerStack(
      this,
      `goosetown-${env}-gameserver`,
      {
        stackName: `goosetown-${env}-gameserver`,
        environment: env,
        vpc: network.vpc,
        cluster: clusterStack.cluster,
        alarmTopic: observability.snsTopic,
      }
    );

    const compute = new ComputeStack(this, `goosetown-${env}-compute`, {
      stackName: `goosetown-${env}-compute`,
      environment: env,
      vpc: network.vpc,
      cluster: clusterStack.cluster,
      tables: database.tables,
      spriteBucket: storage.spriteBucket,
      secrets: auth.secrets,
      certificate: dns.certificate,
      hostedZone: dns.hostedZone,
      gameServerUrl: gameServer.serviceUrl,
      gameServerSg: gameServer.securityGroup,
      alarmTopic: observability.snsTopic,
    });

    new ApiStack(this, `goosetown-${env}-api`, {
      stackName: `goosetown-${env}-api`,
      environment: env,
      connectionsTable: database.tables.connections,
      secrets: auth.secrets,
      fargateTaskRole: compute.service.taskDefinition.taskRole,
      vpc: network.vpc,
      // Pass as string to avoid circular dependency (ApiStack already depends on ComputeStack via fargateTaskRole)
      backendUrl: `https://${canonicalHost("api", env)}`,
      certificate: dns.certificate,
      hostedZone: dns.hostedZone,
    });

    // EventBridge world bus — no deps on other stacks besides env.
    // Deploys after ApiStack to avoid name collisions on the event bus name;
    // functionally independent but logically belongs after the core services.
    const eventing = new EventingStack(this, `goosetown-${env}-eventing`, {
      stackName: `goosetown-${env}-eventing`,
      environment: env,
    });

    // WorldGM service — depends on Cluster, Auth, Eventing.
    // Deploys after Eventing (needs bus ARN) and after Database (tables must
    // exist before the service starts draining them).
    new WorldgmStack(this, `goosetown-${env}-worldgm`, {
      stackName: `goosetown-${env}-worldgm`,
      environment: env,
      cluster: clusterStack.cluster,
      vpc: network.vpc,
      secrets: auth.secrets,
      alarmTopic: observability.snsTopic,
      worldBusArn: eventing.bus.eventBusArn,
      tables: database.tables,
    });

    // Sprite worker — SQS-backed durable pipeline (spec 2026-06-11).
    // Depends on Cluster, Auth, Database, Storage. No dependency on
    // ComputeStack — producer/consumer meet only through the queue's
    // physical name (`goosetown-${env}-sprite-jobs`) at runtime.
    new SpriteWorkerStack(this, `goosetown-${env}-sprite-worker`, {
      stackName: `goosetown-${env}-sprite-worker`,
      environment: env,
      cluster: clusterStack.cluster,
      vpc: network.vpc,
      secrets: auth.secrets,
      tables: database.tables,
      spriteBucket: storage.spriteBucket,
      alarmTopic: observability.snsTopic,
    });

    cdk.Tags.of(this).add("Project", "goosetown");
    cdk.Tags.of(this).add("Environment", env);
  }
}
