#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { NetworkStack } from "./stacks/network-stack";
import { DatabaseStack } from "./stacks/database-stack";
import { StorageStack } from "./stacks/storage-stack";
import { AuthStack } from "./stacks/auth-stack";
import { DnsStack } from "./stacks/dns-stack";
import { ComputeStack } from "./stacks/compute-stack";
import { ApiStack } from "./stacks/api-stack";

const app = new cdk.App();
const env = app.node.tryGetContext("env") || "dev";
const awsEnv = { account: "877352799272", region: "us-east-1" };

// --- Foundation (no cross-stack dependencies) ---

const network = new NetworkStack(app, `goosetown-${env}-network`, {
  env: awsEnv,
  environment: env,
});

const database = new DatabaseStack(app, `goosetown-${env}-database`, {
  env: awsEnv,
  environment: env,
});

const auth = new AuthStack(app, `goosetown-${env}-auth`, {
  env: awsEnv,
  environment: env,
});

// --- DNS (cert must exist before compute and storage can use it) ---

const dns = new DnsStack(app, `goosetown-${env}-dns`, {
  env: awsEnv,
  environment: env,
});

// --- Storage (depends on dns for cert + custom domain) ---

const storage = new StorageStack(app, `goosetown-${env}-storage`, {
  env: awsEnv,
  environment: env,
  certificate: dns.certificate,
  hostedZone: dns.hostedZone,
});

// --- API (depends on database, auth) ---

const api = new ApiStack(app, `goosetown-${env}-api`, {
  env: awsEnv,
  environment: env,
  connectionsTable: database.tables.connections,
  agentsTable: database.tables.agents,
  conversationsTable: database.tables.conversations,
  relationshipsTable: database.tables.relationships,
  secrets: auth.secrets,
});

// --- Compute (depends on network, database, storage, auth, dns, api) ---

const compute = new ComputeStack(app, `goosetown-${env}-compute`, {
  env: awsEnv,
  environment: env,
  vpc: network.vpc,
  tables: database.tables,
  spriteBucket: storage.spriteBucket,
  secrets: auth.secrets,
  certificate: dns.certificate,
  hostedZone: dns.hostedZone,
  wsApiId: api.wsApiId,
});

// Grant Fargate task role permission to push to WebSocket connections
api.grantManageConnections(compute.service.taskDefinition.taskRole);

// --- Tags ---

cdk.Tags.of(app).add("Project", "goosetown");
cdk.Tags.of(app).add("Environment", env);
