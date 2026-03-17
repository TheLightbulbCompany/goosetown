#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { NetworkStack } from "../lib/network-stack";
import { DatabaseStack } from "../lib/database-stack";
import { StorageStack } from "../lib/storage-stack";
import { AuthStack } from "../lib/auth-stack";
import { DnsStack } from "../lib/dns-stack";
import { ComputeStack } from "../lib/compute-stack";

const app = new cdk.App();
const env = app.node.tryGetContext("env") || "dev";
const awsEnv = { account: "877352799272", region: "us-east-1" };

// Network
const network = new NetworkStack(app, `goosetown-${env}-network`, {
  env: awsEnv,
  environment: env,
});

// Database (DynamoDB — no VPC dependency)
const database = new DatabaseStack(app, `goosetown-${env}-database`, {
  env: awsEnv,
  environment: env,
});

// Storage (S3 + CloudFront)
const storage = new StorageStack(app, `goosetown-${env}-storage`, {
  env: awsEnv,
  environment: env,
});

// Auth (Secrets Manager)
const auth = new AuthStack(app, `goosetown-${env}-auth`, {
  env: awsEnv,
  environment: env,
});

// DNS (Route53 + ACM certificate)
// Note: DNS records for ALB and CloudFront are created after compute stack
// by passing references. On first deploy, create DNS stack first to get the cert.
const dns = new DnsStack(app, `goosetown-${env}-dns`, {
  env: awsEnv,
  environment: env,
  // ALB and distribution references will be wired after compute stack exists.
  // For initial deploy, omit these — add them after first successful deploy.
});

// Compute (ECS Fargate + ALB)
const compute = new ComputeStack(app, `goosetown-${env}-compute`, {
  env: awsEnv,
  environment: env,
  vpc: network.vpc,
  tables: database.tables,
  spriteBucket: storage.spriteBucket,
  secrets: auth.secrets,
  certificate: dns.certificate,
});

// Tag all resources
cdk.Tags.of(app).add("Project", "goosetown");
cdk.Tags.of(app).add("Environment", env);
