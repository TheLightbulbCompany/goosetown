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

    this.tables = { agents, instances, relationships, conversations, connections, ownerChats, users, completedGoals };

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
