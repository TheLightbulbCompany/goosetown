/**
 * api-stack.test.ts
 *
 * Asserts that all four Lambda functions (authorizer, connect, disconnect,
 * default) have a 15-second timeout. The authorizer and connect/disconnect
 * functions were raised from 10s to 15s to give headroom for cold-start +
 * JWKS fetch under load.
 *
 * Also asserts that the ManagementApiUrl CfnOutput is still present — it is
 * kept as a rollback safety valve for one prod deploy cycle so an emergency
 * revert to an older ComputeStack revision (which still used Fn.importValue)
 * can resolve the export.
 */

import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as secretsmanager from "aws-cdk-lib/aws-secretsmanager";
import * as fs from "fs";
import * as path from "path";
import { ApiStack } from "../lib/stacks/api-stack";

function buildTestApp(env: "dev" | "prod" = "dev"): { template: Template } {
  const app = new cdk.App();

  const depsStack = new cdk.Stack(app, `deps-${env}`, {
    env: { account: "123456789012", region: "us-east-1" },
  });

  const vpc = ec2.Vpc.fromVpcAttributes(depsStack, "Vpc", {
    vpcId: "vpc-12345",
    availabilityZones: ["us-east-1a", "us-east-1b"],
    privateSubnetIds: ["subnet-private-1a", "subnet-private-1b"],
    publicSubnetIds: ["subnet-public-1a", "subnet-public-1b"],
  });

  const connectionsTable = dynamodb.Table.fromTableName(
    depsStack,
    "ConnectionsTable",
    `goosetown-${env}-connections`
  );

  const makeSecret = (logicalId: string, name: string) =>
    secretsmanager.Secret.fromSecretNameV2(depsStack, logicalId, name);

  const secrets = {
    clerkIssuer: makeSecret("ClerkIssuer", `goosetown-${env}-clerk-issuer`),
    clerkSecretKey: makeSecret(
      "ClerkSecretKey",
      `goosetown-${env}-clerk-secret-key`
    ),
    townTokenSecret: makeSecret(
      "TownTokenSecret",
      `goosetown-${env}-town-token-secret`
    ),
    pixelLabApiKey: makeSecret(
      "PixelLabApiKey",
      `goosetown-${env}-pixellab-api-key`
    ),
  };

  const stack = new ApiStack(app, `goosetown-${env}-api`, {
    stackName: `goosetown-${env}-api`,
    environment: env,
    env: { account: "123456789012", region: "us-east-1" },
    connectionsTable,
    secrets: secrets as any,
    vpc,
    backendUrl: `https://api-${env}.goosetown.isol8.co`,
  });

  return { template: Template.fromStack(stack) };
}

describe("ApiStack — Lambda timeouts", () => {
  let template: Template;

  beforeAll(() => {
    ({ template } = buildTestApp());
  });

  test("all Lambda functions have a 15-second timeout", () => {
    // CDK encodes Duration.seconds(15) as Timeout: 15 in CloudFormation.
    // There are 4 Lambda functions: authorizer, connect, disconnect, default.
    const resources = template.findResources("AWS::Lambda::Function", {
      Properties: {
        Timeout: 15,
      },
    });
    // All 4 functions must have Timeout=15. Capture count as a sanity check.
    const count = Object.keys(resources).length;
    expect(count).toBeGreaterThanOrEqual(4);
  });

  test("authorizer Lambda has timeout 15", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: Match.stringLikeRegexp("ws-authorizer"),
      Timeout: 15,
    });
  });

  test("connect Lambda has timeout 15", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: Match.stringLikeRegexp("ws-connect"),
      Timeout: 15,
    });
  });

  test("disconnect Lambda has timeout 15", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: Match.stringLikeRegexp("ws-disconnect"),
      Timeout: 15,
    });
  });

  test("default-route Lambda has timeout 15", () => {
    template.hasResourceProperties("AWS::Lambda::Function", {
      FunctionName: Match.stringLikeRegexp("ws-default"),
      Timeout: 15,
    });
  });

  test("no Lambda function has a timeout of 10 (old value — regression guard)", () => {
    const resources = template.findResources("AWS::Lambda::Function", {
      Properties: {
        Timeout: 10,
      },
    });
    expect(Object.keys(resources).length).toBe(0);
  });
});

describe("ApiStack — ManagementApiUrl CfnOutput (rollback safety)", () => {
  let template: Template;

  beforeAll(() => {
    ({ template } = buildTestApp());
  });

  test("ManagementApiUrl output exists for emergency rollback compatibility", () => {
    // This output is kept for at least one prod deploy cycle so that an
    // emergency rollback to an older ComputeStack revision (which still used
    // Fn.importValue for goosetown-${env}-management-api-url) can resolve it.
    const outputs = template.findOutputs("ManagementApiUrl");
    expect(Object.keys(outputs).length).toBe(1);
  });

  test("ManagementApiUrl export name follows goosetown-{env}-management-api-url convention", () => {
    const outputs = template.findOutputs("ManagementApiUrl");
    const output = Object.values(outputs)[0] as any;
    expect(output.Export?.Name).toMatch(/goosetown-\w+-management-api-url/);
  });
});

describe("ApiStack — WS Lambdas must NOT be in VPC (regression guard)", () => {
  // Removing VPC config from disconnect + default Lambdas was the
  // architectural fix for the chronic prod-api deploy failures
  // (UPDATE_ROLLBACK_COMPLETE_CLEANUP_IN_PROGRESS triggered by orphan
  // Lambda VPC ENIs that detach asynchronously and outlast CFN's SG
  // delete attempts). These functions only call public AWS services
  // (DynamoDB, the public ALB, Secrets Manager, CloudWatch Logs) and
  // do not need VPC reachability. IAM is the security boundary, not
  // VPC placement.
  //
  // If a future change re-introduces VPC config on the WS Lambdas
  // without first moving to a private-ALB / VPC-endpoint architecture,
  // this test will fail and force a conscious decision.

  test("authorizer Lambda is not in any VPC", () => {
    const { template } = buildTestApp();
    const fns = template.findResources("AWS::Lambda::Function");
    const auth = Object.values(fns).find(
      (r: any) => r.Properties?.FunctionName?.match?.(/ws-authorizer/) || (typeof r.Properties?.FunctionName === "string" && r.Properties.FunctionName.includes("ws-authorizer"))
    ) as any;
    expect(auth).toBeDefined();
    expect(auth.Properties.VpcConfig).toBeUndefined();
  });

  test("connect Lambda is not in any VPC", () => {
    const { template } = buildTestApp();
    const fns = template.findResources("AWS::Lambda::Function");
    const fn = Object.values(fns).find(
      (r: any) => typeof r.Properties?.FunctionName === "string" && r.Properties.FunctionName.includes("ws-connect")
    ) as any;
    expect(fn).toBeDefined();
    expect(fn.Properties.VpcConfig).toBeUndefined();
  });

  test("disconnect Lambda is not in any VPC", () => {
    const { template } = buildTestApp();
    const fns = template.findResources("AWS::Lambda::Function");
    const fn = Object.values(fns).find(
      (r: any) => typeof r.Properties?.FunctionName === "string" && r.Properties.FunctionName.includes("ws-disconnect")
    ) as any;
    expect(fn).toBeDefined();
    expect(fn.Properties.VpcConfig).toBeUndefined();
  });

  test("default-route Lambda is not in any VPC", () => {
    const { template } = buildTestApp();
    const fns = template.findResources("AWS::Lambda::Function");
    const fn = Object.values(fns).find(
      (r: any) => typeof r.Properties?.FunctionName === "string" && r.Properties.FunctionName.includes("ws-default")
    ) as any;
    expect(fn).toBeDefined();
    expect(fn.Properties.VpcConfig).toBeUndefined();
  });
});

describe("ApiStack — prod-only legacy WS export preservation", () => {
  // The deployed prod-api template (synthed under an older CDK) carried an
  // 8-char auto-export hash: ExportsOutputRefWebSocketApi6DDF9954. Today's
  // CDK derives a 16-char hash for the same construct path, so a fresh synth
  // omits the legacy name and CFN refuses to drop it (still imported by
  // goosetown-prod-compute). The explicit prod-only CfnOutput keeps the
  // legacy name alive so prod-api can update independently.

  test("prod template exposes the legacy 8-char-hash export", () => {
    const { template } = buildTestApp("prod");
    const outputs = template.findOutputs("LegacyWsApiAutoExportProd");
    expect(Object.keys(outputs).length).toBe(1);
    const output = Object.values(outputs)[0] as any;
    // Must match the literal export name CFN sees on the deployed prod-api
    // stack, captured 2026-05-02 via describe-stacks.
    expect(output.Export?.Name).toBe(
      "goosetown-prod-api:ExportsOutputRefWebSocketApi6DDF9954"
    );
  });

  test("dev template does NOT emit the prod-only legacy export", () => {
    // Dev's deployed compute imports the 16-char-hash form (matches today's
    // CDK auto-export), so no preservation is needed; emitting it on dev
    // would collide with the auto-gen export name.
    const { template } = buildTestApp("dev");
    const outputs = template.findOutputs("LegacyWsApiAutoExportProd");
    expect(Object.keys(outputs).length).toBe(0);
  });
});

describe("ApiStack — authorizer token-source migration", () => {
  // The authorizer Lambda code lives in apps/infra/lambda/ws-authorizer/index.py
  // (moved from inline Code.fromInline in api-stack.ts to a file-based asset).
  // These tests are file-content regression guards for the migration that moved
  // the daemon's town-token from a query string (?token=…) to an
  // Authorization header. Tokens in URLs leak into proxy/access logs; the
  // header keeps them out. Query-string fallback is retained for daemons
  // running an older skill bundle.
  const authorizerSource = fs.readFileSync(
    path.join(__dirname, "..", "lambda", "ws-authorizer", "index.py"),
    "utf8"
  );

  test("authorizer extracts token from Authorization header", () => {
    expect(authorizerSource).toMatch(/k\.lower\(\) == "authorization"/);
    expect(authorizerSource).toMatch(/Bearer/);
  });

  test("authorizer keeps query-string fallback during rollout", () => {
    expect(authorizerSource).toMatch(/queryStringParameters/);
    // Should still grab token from query when header is absent.
    expect(authorizerSource).toMatch(/qs\.get\("token"\)/);
  });

  test("authorizer no longer dumps the full event (which leaks tokens)", () => {
    // Old pattern used to log the entire event — including queryStringParameters,
    // including the token. Confirm that's gone.
    expect(authorizerSource).not.toMatch(/Authorizer event: %s.*json\.dumps\(event/);
  });
});
