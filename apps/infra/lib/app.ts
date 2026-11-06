#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { GooseTownStage } from "./goosetown-stage";

// Stack names remain explicit in GooseTownStage. Deployment is disabled pending
// resource adoption, IAM isolation and an independently reviewed change set.
const app = new cdk.App();
const awsEnv = { account: "877352799272", region: "us-east-1" };
new GooseTownStage(app, "dev", { env: awsEnv, environment: "dev" });
new GooseTownStage(app, "prod", { env: awsEnv, environment: "prod" });
app.synth();
