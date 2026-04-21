import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import { NetworkStack } from "./stacks/network-stack";
import { DatabaseStack } from "./stacks/database-stack";
import { AuthStack } from "./stacks/auth-stack";
import { DnsStack } from "./stacks/dns-stack";
import { StorageStack } from "./stacks/storage-stack";
import { ComputeStack } from "./stacks/compute-stack";
import { GameServerStack } from "./stacks/gameserver-stack";
import { ApiStack } from "./stacks/api-stack";
import { ObservabilityStack } from "./stacks/observability-stack";

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

    const gameServer = new GameServerStack(
      this,
      `goosetown-${env}-gameserver`,
      {
        stackName: `goosetown-${env}-gameserver`,
        environment: env,
        vpc: network.vpc,
        alarmTopic: observability.snsTopic,
      }
    );

    const compute = new ComputeStack(this, `goosetown-${env}-compute`, {
      stackName: `goosetown-${env}-compute`,
      environment: env,
      vpc: network.vpc,
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
      backendUrl: `https://api-${env}.goosetown.isol8.co`,
      certificate: dns.certificate,
      hostedZone: dns.hostedZone,
    });

    cdk.Tags.of(this).add("Project", "goosetown");
    cdk.Tags.of(this).add("Environment", env);
  }
}
