#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { ShellStep } from "aws-cdk-lib/pipelines";
import {
  AwsCredentials,
  GitHubWorkflow,
  GitHubActionStep,
  StackCapabilities,
  JobPermission,
} from "cdk-pipelines-github";
import { GooseTownStage } from "./goosetown-stage";

const app = new cdk.App();
const awsEnv = { account: "877352799272", region: "us-east-1" };

// Vercel env vars used in all Vercel deploy steps
const vercelEnv = {
  VERCEL_TOKEN: "${{ secrets.VERCEL_TOKEN }}",
  VERCEL_ORG_ID: "${{ secrets.VERCEL_ORG_ID }}",
  VERCEL_PROJECT_ID: "${{ secrets.VERCEL_PROJECT_ID }}",
};

// ---------------------------------------------------------------------------
// CDK Pipeline (generates .github/workflows/deploy.yml)
// ---------------------------------------------------------------------------
const pipeline = new GitHubWorkflow(app, "goosetown-pipeline", {
  synth: new ShellStep("Synth", {
    installCommands: [
      "corepack enable",
      "corepack prepare pnpm@latest --activate",
    ],
    commands: [
      "cd apps/infra",
      "pnpm install --frozen-lockfile",
      "npx cdk synth",
    ],
    primaryOutputDirectory: "apps/infra/cdk.out",
  }),
  awsCreds: AwsCredentials.fromOpenIdConnect({
    gitHubActionRoleArn: "arn:aws:iam::877352799272:role/goosetown-github-actions",
  }),
  workflowPath: "../../.github/workflows/deploy.yml",
});

// ---------------------------------------------------------------------------
// Dev stage — auto-deploy infra + frontend on merge to main
// ---------------------------------------------------------------------------
pipeline.addStageWithGitHubOptions(
  new GooseTownStage(app, "dev", { env: awsEnv, environment: "dev" }),
  {
    stackCapabilities: [
      StackCapabilities.NAMED_IAM,
      StackCapabilities.AUTO_EXPAND,
    ],
    post: [
      new GitHubActionStep("DeployVercelDev", {
        jobSteps: [
          { name: "Checkout", uses: "actions/checkout@v4" },
          { name: "Setup pnpm", uses: "pnpm/action-setup@v4" },
          { name: "Setup Node.js", uses: "actions/setup-node@v4", with: { "node-version": "20", cache: "pnpm" } },
          { name: "Install Vercel CLI", run: "npm install -g vercel" },
          { name: "Pull Vercel Settings", run: "vercel pull --yes --token=$VERCEL_TOKEN", env: vercelEnv },
          {
            name: "Build Frontend",
            run: "vercel build --token=$VERCEL_TOKEN",
            env: {
              ...vercelEnv,
              VITE_CLERK_PUBLISHABLE_KEY: "${{ secrets.VITE_CLERK_PUBLISHABLE_KEY_DEV }}",
              VITE_BACKEND_URL: "https://api-dev.goosetown.isol8.co/api/v1",
              VITE_WS_URL: "wss://ws-dev.goosetown.isol8.co",
            },
          },
          {
            name: "Deploy to Vercel (Preview)",
            id: "vercel-deploy-dev",
            run: 'DEPLOY_URL=$(vercel deploy --prebuilt --token=$VERCEL_TOKEN) && echo "DEPLOY_URL=$DEPLOY_URL" >> $GITHUB_OUTPUT',
            env: vercelEnv,
          },
          {
            name: "Alias to dev.goosetown.isol8.co",
            run: "vercel alias ${{ steps.vercel-deploy-dev.outputs.DEPLOY_URL }} dev.goosetown.isol8.co --token=$VERCEL_TOKEN",
            env: vercelEnv,
          },
          {
            // Regression guard: the sprite CDN must carry
            // Cross-Origin-Resource-Policy: cross-origin so <img> tags on
            // the frontend (served COEP:require-corp for Godot WebGL) can
            // load them. A missing header silently breaks all agent
            // sprites and was only caught once by humans eyeballing the
            // sidebar. Curl a (possibly 404) asset — CloudFront applies
            // the ResponseHeadersPolicy to all responses including errors.
            name: "Smoke-test sprite CORP header",
            run: [
              'HEADERS=$(curl -sI "https://assets-dev.goosetown.isol8.co/_corp-check.png")',
              'echo "$HEADERS" | grep -qi "^cross-origin-resource-policy: *cross-origin" || { echo "FAIL: CORP header missing or wrong on sprite CDN"; echo "$HEADERS"; exit 1; }',
              'echo "OK: sprite CDN has Cross-Origin-Resource-Policy: cross-origin"',
            ].join("\n"),
          },
        ],
      }),
    ],
  },
);

// ---------------------------------------------------------------------------
// Prod stage — auto-deploys after dev succeeds.
// (The manual approval gate was removed 2026-04-19; reinstate by passing
// `pre: [approvalStep]` to addStageWithGitHubOptions when a gate is needed.)
// ---------------------------------------------------------------------------
pipeline.addStageWithGitHubOptions(
  new GooseTownStage(app, "prod", { env: awsEnv, environment: "prod" }),
  {
    stackCapabilities: [
      StackCapabilities.NAMED_IAM,
      StackCapabilities.AUTO_EXPAND,
    ],
    post: [
      new GitHubActionStep("DeployVercelProd", {
        jobSteps: [
          { name: "Checkout", uses: "actions/checkout@v4" },
          { name: "Setup pnpm", uses: "pnpm/action-setup@v4" },
          { name: "Setup Node.js", uses: "actions/setup-node@v4", with: { "node-version": "20", cache: "pnpm" } },
          { name: "Install Vercel CLI", run: "npm install -g vercel" },
          { name: "Pull Vercel Settings", run: "vercel pull --yes --environment=production --token=$VERCEL_TOKEN", env: vercelEnv },
          {
            name: "Build Frontend (Production)",
            run: "vercel build --prod --token=$VERCEL_TOKEN",
            env: {
              ...vercelEnv,
              VITE_CLERK_PUBLISHABLE_KEY: "${{ secrets.VITE_CLERK_PUBLISHABLE_KEY_PROD }}",
              VITE_BACKEND_URL: "https://api-prod.goosetown.isol8.co/api/v1",
              VITE_WS_URL: "wss://ws-prod.goosetown.isol8.co",
            },
          },
          {
            name: "Deploy to Vercel (Production)",
            id: "vercel-deploy-prod",
            run: 'DEPLOY_URL=$(vercel deploy --prebuilt --prod --token=$VERCEL_TOKEN) && echo "DEPLOY_URL=$DEPLOY_URL" >> $GITHUB_OUTPUT',
            env: vercelEnv,
          },
          {
            name: "Alias to goosetown.isol8.co",
            run: "vercel alias ${{ steps.vercel-deploy-prod.outputs.DEPLOY_URL }} goosetown.isol8.co --token=$VERCEL_TOKEN",
            env: vercelEnv,
          },
        ],
      }),
    ],
  },
);

app.synth();
