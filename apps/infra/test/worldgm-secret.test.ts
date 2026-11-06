import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as ecs from "aws-cdk-lib/aws-ecs";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import { WorldgmStack } from "../lib/stacks/worldgm-stack";

test.each(["dev", "prod"])("%s WorldGM stays paused and has no Clerk master key reference", (env) => {
  const app = new cdk.App();
  const deps = new cdk.Stack(app, `deps-${env}`, { env: { account: "123456789012", region: "us-east-1" } });
  const vpc = ec2.Vpc.fromVpcAttributes(deps, "Vpc", {
    vpcId: "vpc-12345", availabilityZones: ["us-east-1a"],
    privateSubnetIds: ["subnet-12345"], publicSubnetIds: ["subnet-23456"],
  });
  const cluster = new ecs.Cluster(deps, "Cluster", { vpc });
  const table = (n: string) => dynamodb.Table.fromTableName(deps, n, `${env}-${n}`);
  const secret = (n: string) => secretsmanager.Secret.fromSecretNameV2(deps, n, `${env}-${n}`);
  const tables = Object.fromEntries(["agents", "worldgmArcs", "worldEvents", "conversations", "relationships", "memories"].map(n => [n, table(n)]));
  const secrets = Object.fromEntries(["clerkIssuer", "clerkSecretKey", "townTokenSecret"].map(n => [n, secret(n)]));
  const stack = new WorldgmStack(app, `worldgm-${env}`, {
    env: { account: "123456789012", region: "us-east-1" }, environment: env,
    cluster, vpc, tables: tables as any, secrets: secrets as any,
    worldBusArn: "arn:aws:events:us-east-1:123456789012:event-bus/synthetic",
  });
  const template = Template.fromStack(stack);
  template.hasResourceProperties("AWS::ECS::Service", { DesiredCount: 0 });
  const synthesized = JSON.stringify(template.toJSON());
  expect(synthesized).not.toContain("CLERK_SECRET_KEY");
  expect(synthesized).not.toContain(`${env}-clerkSecretKey`);
  expect(synthesized).toContain("CLERK_ISSUER");
  expect(synthesized).toContain("TOWN_TOKEN_SECRET");
});
