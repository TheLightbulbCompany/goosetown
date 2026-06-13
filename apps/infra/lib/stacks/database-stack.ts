import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import { Construct } from "constructs";

export interface DatabaseStackProps extends cdk.StackProps {
  environment: string;
}

export interface DatabaseTables {
  agents: dynamodb.Table;
  instances: dynamodb.Table;
  relationships: dynamodb.Table;
  conversations: dynamodb.Table;
  connections: dynamodb.Table;
  ownerChats: dynamodb.Table;
  users: dynamodb.Table;
  completedGoals: dynamodb.Table;
  worldgmArcs: dynamodb.Table;
  worldEvents: dynamodb.Table;
  jobs: dynamodb.Table;
  ledger: dynamodb.Table;
  providerSubscriptions: dynamodb.Table;
}

export class DatabaseStack extends cdk.Stack {
  public readonly tables: DatabaseTables;

  constructor(scope: Construct, id: string, props: DatabaseStackProps) {
    super(scope, id, props);

    const env = props.environment;
    const removalPolicy =
      env === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY;

    // Agents table
    const agents = new dynamodb.Table(this, "AgentsTable", {
      tableName: `goosetown-${env}-agents`,
      partitionKey: { name: "id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: env === "prod",
      },
    });

    agents.addGlobalSecondaryIndex({
      indexName: "userId-agentName-index",
      partitionKey: { name: "userId", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "agentName", type: dynamodb.AttributeType.STRING },
    });

    agents.addGlobalSecondaryIndex({
      indexName: "instanceId-index",
      partitionKey: {
        name: "instanceId",
        type: dynamodb.AttributeType.STRING,
      },
    });

    agents.addGlobalSecondaryIndex({
      indexName: "isActive-joinedAt-index",
      partitionKey: { name: "isActive", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "joinedAt", type: dynamodb.AttributeType.STRING },
    });

    // Instances table
    const instances = new dynamodb.Table(this, "InstancesTable", {
      tableName: `goosetown-${env}-instances`,
      partitionKey: { name: "id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy,
    });

    instances.addGlobalSecondaryIndex({
      indexName: "userId-index",
      partitionKey: { name: "userId", type: dynamodb.AttributeType.STRING },
    });

    instances.addGlobalSecondaryIndex({
      indexName: "townToken-index",
      partitionKey: { name: "townToken", type: dynamodb.AttributeType.STRING },
    });

    // Relationships table
    const relationships = new dynamodb.Table(this, "RelationshipsTable", {
      tableName: `goosetown-${env}-relationships`,
      partitionKey: { name: "agentAId", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "agentBId", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy,
    });

    // Conversations table
    const conversations = new dynamodb.Table(this, "ConversationsTable", {
      tableName: `goosetown-${env}-conversations`,
      partitionKey: { name: "id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy,
    });

    conversations.addGlobalSecondaryIndex({
      indexName: "status-startedAt-index",
      partitionKey: { name: "status", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "startedAt", type: dynamodb.AttributeType.STRING },
    });

    conversations.addGlobalSecondaryIndex({
      indexName: "participantA-endedAt-index",
      partitionKey: {
        name: "participantAId",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: { name: "endedAt", type: dynamodb.AttributeType.STRING },
    });

    conversations.addGlobalSecondaryIndex({
      indexName: "status-updatedAt-index",
      partitionKey: { name: "status", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "updatedAt", type: dynamodb.AttributeType.STRING },
    });

    // Connections table (WebSocket state)
    const connections = new dynamodb.Table(this, "ConnectionsTable", {
      tableName: `goosetown-${env}-connections`,
      partitionKey: {
        name: "connectionId",
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy,
    });

    // Owner chats table (private owner ↔ agent DMs)
    const ownerChats = new dynamodb.Table(this, "OwnerChatsTable", {
      tableName: `goosetown-${env}-owner-chats`,
      partitionKey: { name: "id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: env === "prod",
      },
      removalPolicy,
    });
    ownerChats.addGlobalSecondaryIndex({
      indexName: "agentId-createdAt-index",
      partitionKey: { name: "agentId", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "createdAt", type: dynamodb.AttributeType.NUMBER },
    });

    // Users table (per-user state: quota counters, token revocation timestamp)
    const users = new dynamodb.Table(this, "UsersTable", {
      tableName: `goosetown-${env}-users`,
      partitionKey: { name: "userId", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: env === "prod",
      },
      removalPolicy,
    });

    // Completed goals table (history of goals an agent has finished)
    const completedGoals = new dynamodb.Table(this, "CompletedGoalsTable", {
      tableName: `goosetown-${env}-completed-goals`,
      partitionKey: { name: "agentId", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "goalId", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: env === "prod",
      },
      removalPolicy,
    });
    completedGoals.addGlobalSecondaryIndex({
      indexName: "agentId-completedAt-index",
      partitionKey: { name: "agentId", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "completedAt", type: dynamodb.AttributeType.STRING },
    });

    // worldgm-arcs — narrative arc state (WorldGM service reads/writes)
    const worldgmArcs = new dynamodb.Table(this, "WorldgmArcsTable", {
      tableName: `goosetown-${env}-worldgm-arcs`,
      partitionKey: { name: "arcId", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: env === "prod",
      },
      removalPolicy,
    });
    worldgmArcs.addGlobalSecondaryIndex({
      indexName: "status-nextBeatAt-index",
      partitionKey: { name: "status", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "nextBeatAt", type: dynamodb.AttributeType.STRING },
    });

    // world-events — durable event log for Tier-1 frontend feed (Plan 4 reads this)
    const worldEvents = new dynamodb.Table(this, "WorldEventsTable", {
      tableName: `goosetown-${env}-world-events`,
      partitionKey: { name: "eventId", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: env === "prod",
      },
      removalPolicy,
    });
    worldEvents.addGlobalSecondaryIndex({
      indexName: "byTimestamp",
      // envBucket is a constant partition key (e.g., "dev" or "prod") that
      // keeps all events for one environment in a single GSI partition,
      // enabling a simple scan-by-time query for the feed.
      partitionKey: { name: "envBucket", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "timestamp", type: dynamodb.AttributeType.STRING },
    });

    // jobs — GooseTown Exchange work postings (bid map embedded; contract merged in)
    const jobs = new dynamodb.Table(this, "JobsTable", {
      tableName: `goosetown-${env}-jobs`,
      partitionKey: { name: "id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: env === "prod",
      },
      removalPolicy,
    });
    jobs.addGlobalSecondaryIndex({
      indexName: "status-createdAt-index",
      partitionKey: { name: "status", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "createdAt", type: dynamodb.AttributeType.STRING },
    });

    // ledger — append-only crumb movements (Exchange settlement discipline)
    const ledger = new dynamodb.Table(this, "LedgerTable", {
      tableName: `goosetown-${env}-ledger`,
      partitionKey: { name: "id", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: env === "prod",
      },
      removalPolicy,
    });
    ledger.addGlobalSecondaryIndex({
      indexName: "agentId-createdAt-index",
      partitionKey: { name: "agentId", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "createdAt", type: dynamodb.AttributeType.STRING },
    });

    // provider-subscriptions — membership projection for the ProviderDirectory seam (ADR-0008)
    // PK: category (S) — AEX category slug (or wildcard prefix)
    // SK: providerSort (S) — f"{trustScore:08.4f}#{providerId}" enables trust-desc query via ScanIndexForward=False
    // GSI providerId-index: PK providerId (S) — allows register/unregister to find-and-delete all rows for a provider
    const providerSubscriptions = new dynamodb.Table(this, "ProviderSubscriptionsTable", {
      tableName: `goosetown-${env}-provider-subscriptions`,
      partitionKey: { name: "category", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "providerSort", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: {
        pointInTimeRecoveryEnabled: env === "prod",
      },
      removalPolicy,
    });
    providerSubscriptions.addGlobalSecondaryIndex({
      indexName: "providerId-index",
      partitionKey: { name: "providerId", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.tables = { agents, instances, relationships, conversations, connections, ownerChats, users, completedGoals, worldgmArcs, worldEvents, jobs, ledger, providerSubscriptions };

    // Outputs
    new cdk.CfnOutput(this, "AgentsTableName", {
      value: agents.tableName,
    });
    new cdk.CfnOutput(this, "InstancesTableName", {
      value: instances.tableName,
    });
    new cdk.CfnOutput(this, "ConnectionsTableName", {
      value: connections.tableName,
    });
  }
}
