#!/usr/bin/env node
import * as cdk from "aws-cdk-lib";
import { ShellStep } from "aws-cdk-lib/pipelines";
import {
  AwsCredentials,
  GitHubWorkflow,
  GitHubActionStep,
  StackCapabilities,
  JobPermission,
  JsonPatch,
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
const makePipeline = (name: string, workflowPath: string) => new GitHubWorkflow(app, name, {
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
  workflowPath,
  workflowTriggers: { workflowDispatch: {} },
});
const devPipeline = makePipeline("goosetown-dev-pipeline", "../../.github/workflows/deploy.yml");
const prodPipeline = makePipeline("goosetown-prod-pipeline", "../../.github/workflows/deploy-prod.yml");
// Every generated asset and deploy job depends on Build-Synth. Guard it before
// any credentialed job, in addition to the environment on production stages.
devPipeline.workflowFile.patch(JsonPatch.add("/jobs/Build-Synth/if", "${{ github.ref == 'refs/heads/main' }}"));
prodPipeline.workflowFile.patch(
  JsonPatch.add("/jobs/Build-Synth/if", "${{ github.ref == 'refs/heads/main' }}"),
  JsonPatch.add("/jobs/Build-Synth/environment", "Production"),
);

// ---------------------------------------------------------------------------
// Dev stage — manual dev-only deployment
// ---------------------------------------------------------------------------
devPipeline.addStageWithGitHubOptions(
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
              VITE_BACKEND_URL: "https://api-dev.goosetown.thelightbulb.company/api/v1",
              VITE_WS_URL: "wss://ws-dev.goosetown.thelightbulb.company",
            },
          },
          {
            // Tag this preview with its source commit for release provenance.
            name: "Deploy to Vercel (Preview)",
            id: "vercel-deploy-dev",
            run: 'DEPLOY_URL=$(vercel deploy --prebuilt --meta githubCommitSha=$GITHUB_SHA --token=$VERCEL_TOKEN) && echo "DEPLOY_URL=$DEPLOY_URL" >> $GITHUB_OUTPUT',
            env: vercelEnv,
          },
          {
            name: "Alias to dev.goosetown.thelightbulb.company",
            run: "vercel alias ${{ steps.vercel-deploy-dev.outputs.DEPLOY_URL }} dev.goosetown.thelightbulb.company --token=$VERCEL_TOKEN",
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
              'HEADERS=$(curl -sI "https://assets-dev.goosetown.thelightbulb.company/_corp-check.png")',
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
// Production has a separate manual workflow and Production environment.
// Environment protection, OIDC trust and dev alias safety must be verified before enabling.
// ---------------------------------------------------------------------------
prodPipeline.addStageWithGitHubOptions(
  new GooseTownStage(app, "prod", { env: awsEnv, environment: "prod" }),
  {
    gitHubEnvironment: { name: "Production" },
    stackCapabilities: [
      StackCapabilities.NAMED_IAM,
      StackCapabilities.AUTO_EXPAND,
    ],
    post: [
      new GitHubActionStep("DeployVercelProd", {
        githubEnvironment: "Production",
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
              VITE_BACKEND_URL: "https://api.goosetown.thelightbulb.company/api/v1",
              VITE_WS_URL: "wss://ws.goosetown.thelightbulb.company",
            },
          },
          {
            name: "Deploy to Vercel (Production)",
            id: "vercel-deploy-prod",
            run: 'DEPLOY_URL=$(vercel deploy --prebuilt --prod --token=$VERCEL_TOKEN) && echo "DEPLOY_URL=$DEPLOY_URL" >> $GITHUB_OUTPUT',
            env: vercelEnv,
          },
          {
            // Production domain; dev alias must be independently protected before enabling.
            name: "Alias to goosetown.thelightbulb.company",
            run: "vercel alias ${{ steps.vercel-deploy-prod.outputs.DEPLOY_URL }} goosetown.thelightbulb.company --token=$VERCEL_TOKEN",
            env: vercelEnv,
          },
        ],
      }),
    ],
  },
);

app.synth();
