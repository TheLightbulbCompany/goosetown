/**
 * database-stack.test.ts
 *
 * Exchange tables: verify that the jobs and ledger DynamoDB tables are
 * synthesised with the correct primary keys and GSIs.
 */

import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { DatabaseStack } from "../lib/stacks/database-stack";

function buildTestApp(env: string): Template {
  const app = new cdk.App();
  const stack = new DatabaseStack(app, `goosetown-${env}-database`, {
    stackName: `goosetown-${env}-database`,
    environment: env,
    env: { account: "123456789012", region: "us-east-1" },
  });
  return Template.fromStack(stack);
}

describe("DatabaseStack — jobs table (GooseTown Exchange)", () => {
  test("dev jobs table exists with correct name and PK", () => {
    const template = buildTestApp("dev");
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "goosetown-dev-jobs",
      KeySchema: Match.arrayWith([
        Match.objectLike({ AttributeName: "id", KeyType: "HASH" }),
      ]),
      BillingMode: "PAY_PER_REQUEST",
    });
  });

  test("prod jobs table exists with correct name", () => {
    const template = buildTestApp("prod");
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "goosetown-prod-jobs",
      BillingMode: "PAY_PER_REQUEST",
    });
  });

  test("jobs table has status-createdAt-index GSI", () => {
    const template = buildTestApp("dev");
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "goosetown-dev-jobs",
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "status-createdAt-index",
          KeySchema: Match.arrayWith([
            Match.objectLike({ AttributeName: "status", KeyType: "HASH" }),
            Match.objectLike({ AttributeName: "createdAt", KeyType: "RANGE" }),
          ]),
        }),
      ]),
    });
  });

  test("prod jobs table has PITR enabled", () => {
    const template = buildTestApp("prod");
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "goosetown-prod-jobs",
      PointInTimeRecoverySpecification: {
        PointInTimeRecoveryEnabled: true,
      },
    });
  });

  test("dev jobs table does NOT have PITR enabled", () => {
    const template = buildTestApp("dev");
    const tables = template.findResources("AWS::DynamoDB::Table");
    const jobsTable = Object.values(tables).find(
      (t: any) => t?.Properties?.TableName === "goosetown-dev-jobs"
    ) as any;
    const pitr =
      jobsTable?.Properties?.PointInTimeRecoverySpecification
        ?.PointInTimeRecoveryEnabled;
    expect(pitr).toBeFalsy();
  });
});

describe("DatabaseStack — ledger table (GooseTown Exchange)", () => {
  test("dev ledger table exists with correct name and PK", () => {
    const template = buildTestApp("dev");
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "goosetown-dev-ledger",
      KeySchema: Match.arrayWith([
        Match.objectLike({ AttributeName: "id", KeyType: "HASH" }),
      ]),
      BillingMode: "PAY_PER_REQUEST",
    });
  });

  test("prod ledger table exists with correct name", () => {
    const template = buildTestApp("prod");
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "goosetown-prod-ledger",
      BillingMode: "PAY_PER_REQUEST",
    });
  });

  test("ledger table has agentId-createdAt-index GSI", () => {
    const template = buildTestApp("dev");
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "goosetown-dev-ledger",
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "agentId-createdAt-index",
          KeySchema: Match.arrayWith([
            Match.objectLike({ AttributeName: "agentId", KeyType: "HASH" }),
            Match.objectLike({ AttributeName: "createdAt", KeyType: "RANGE" }),
          ]),
        }),
      ]),
    });
  });

  test("prod ledger table has PITR enabled", () => {
    const template = buildTestApp("prod");
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "goosetown-prod-ledger",
      PointInTimeRecoverySpecification: {
        PointInTimeRecoveryEnabled: true,
      },
    });
  });

  test("dev ledger table does NOT have PITR enabled", () => {
    const template = buildTestApp("dev");
    const tables = template.findResources("AWS::DynamoDB::Table");
    const ledgerTable = Object.values(tables).find(
      (t: any) => t?.Properties?.TableName === "goosetown-dev-ledger"
    ) as any;
    const pitr =
      ledgerTable?.Properties?.PointInTimeRecoverySpecification
        ?.PointInTimeRecoveryEnabled;
    expect(pitr).toBeFalsy();
  });
});

describe("DatabaseStack — memories table (Phase 2 resident memory)", () => {
  test("dev memories table exists with correct name, PK and SK", () => {
    const template = buildTestApp("dev");
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "goosetown-dev-memories",
      KeySchema: Match.arrayWith([
        Match.objectLike({ AttributeName: "agentId", KeyType: "HASH" }),
        Match.objectLike({ AttributeName: "createdAt", KeyType: "RANGE" }),
      ]),
      BillingMode: "PAY_PER_REQUEST",
    });
  });

  test("prod memories table exists with correct name", () => {
    const template = buildTestApp("prod");
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "goosetown-prod-memories",
      BillingMode: "PAY_PER_REQUEST",
    });
  });

  test("memories table has agentId-subject-index GSI with PK agentId, SK subject, projection ALL", () => {
    const template = buildTestApp("dev");
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "goosetown-dev-memories",
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "agentId-subject-index",
          KeySchema: Match.arrayWith([
            Match.objectLike({ AttributeName: "agentId", KeyType: "HASH" }),
            Match.objectLike({ AttributeName: "subject", KeyType: "RANGE" }),
          ]),
          Projection: Match.objectLike({ ProjectionType: "ALL" }),
        }),
      ]),
    });
  });

  test("prod memories table has PITR enabled", () => {
    const template = buildTestApp("prod");
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "goosetown-prod-memories",
      PointInTimeRecoverySpecification: {
        PointInTimeRecoveryEnabled: true,
      },
    });
  });

  test("dev memories table does NOT have PITR enabled", () => {
    const template = buildTestApp("dev");
    const tables = template.findResources("AWS::DynamoDB::Table");
    const memoriesTable = Object.values(tables).find(
      (t: any) => t?.Properties?.TableName === "goosetown-dev-memories"
    ) as any;
    const pitr =
      memoriesTable?.Properties?.PointInTimeRecoverySpecification
        ?.PointInTimeRecoveryEnabled;
    expect(pitr).toBeFalsy();
  });
});

describe("DatabaseStack — provider-subscriptions table (Wave 2 ProviderDirectory)", () => {
  test("dev table exists with correct name, PK and SK", () => {
    const template = buildTestApp("dev");
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "goosetown-dev-provider-subscriptions",
      KeySchema: Match.arrayWith([
        Match.objectLike({ AttributeName: "category", KeyType: "HASH" }),
        Match.objectLike({ AttributeName: "providerSort", KeyType: "RANGE" }),
      ]),
      BillingMode: "PAY_PER_REQUEST",
    });
  });

  test("prod table exists with correct name", () => {
    const template = buildTestApp("prod");
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "goosetown-prod-provider-subscriptions",
      BillingMode: "PAY_PER_REQUEST",
    });
  });

  test("table has providerId-index GSI with PK providerId", () => {
    const template = buildTestApp("dev");
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "goosetown-dev-provider-subscriptions",
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: "providerId-index",
          KeySchema: Match.arrayWith([
            Match.objectLike({ AttributeName: "providerId", KeyType: "HASH" }),
          ]),
          Projection: Match.objectLike({ ProjectionType: "ALL" }),
        }),
      ]),
    });
  });

  test("prod table has PITR enabled", () => {
    const template = buildTestApp("prod");
    template.hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: "goosetown-prod-provider-subscriptions",
      PointInTimeRecoverySpecification: {
        PointInTimeRecoveryEnabled: true,
      },
    });
  });

  test("dev table does NOT have PITR enabled", () => {
    const template = buildTestApp("dev");
    const tables = template.findResources("AWS::DynamoDB::Table");
    const subsTable = Object.values(tables).find(
      (t: any) =>
        t?.Properties?.TableName === "goosetown-dev-provider-subscriptions"
    ) as any;
    const pitr =
      subsTable?.Properties?.PointInTimeRecoverySpecification
        ?.PointInTimeRecoveryEnabled;
    expect(pitr).toBeFalsy();
  });
});


describe("production table recovery coverage", () => {
  test("every production table enables PITR while dev remains disabled", () => {
    for (const env of ["prod", "dev"]) {
      const tables = Object.values(buildTestApp(env).findResources("AWS::DynamoDB::Table")) as any[];
      expect(tables).toHaveLength(14);
      for (const table of tables) {
        expect(table.Properties.PointInTimeRecoverySpecification?.PointInTimeRecoveryEnabled ?? false)
          .toBe(env === "prod");
      }
    }
  });
});
