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
            // Tag the Preview deploy with this workflow run's commit SHA so
            // the prod-side re-alias step (see DeployVercelProd) can find
            // *this exact* deploy via the Vercel metadata filter rather
            // than picking up an unrelated Preview from a parallel PR or
            // manual deploy.
            name: "Deploy to Vercel (Preview)",
            id: "vercel-deploy-dev",
            run: 'DEPLOY_URL=$(vercel deploy --prebuilt --meta githubCommitSha=$GITHUB_SHA --token=$VERCEL_TOKEN) && echo "DEPLOY_URL=$DEPLOY_URL" >> $GITHUB_OUTPUT',
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
            // Root cause: `dev.goosetown.isol8.co` is registered on the
            // Vercel project with `gitBranch=null`, which makes Vercel
            // auto-alias it to every Production deploy. So the dev-target
            // Preview built in DeployVercelDev (the right bundle for dev)
            // gets silently overwritten by the Production deploy that
            // just ran above, leaving dev pointed at the prod-target
            // bundle with prod API URLs baked in. Symptom: "Failed to
            // fetch" everywhere on dev, conversations sidebar empty,
            // agents can't connect.
            //
            // This step re-pins dev back to the Preview deploy from this
            // workflow run. We run it BEFORE the apex alias step (which
            // is known to fail intermittently on cert issuance — see
            // docs/superpowers/test-runs/2026-05-06-prod-restoration-
            // summary.md) so dev correctness doesn't depend on the prod
            // apex alias succeeding.
            //
            // Permanent fix is one Vercel API call to set `gitBranch` on
            // the dev domain so prod deploys don't auto-alias it.
            name: "Re-alias dev.goosetown.isol8.co (workaround for prod auto-alias clobber)",
            run: [
              // Filter by the metadata key the dev step stamped on the
              // Preview deploy, so we pin to *this workflow run's* dev
              // build — not whatever Preview happens to be newest at the
              // Vercel project level (which could be from a parallel PR
              // or manual deploy and would silently mis-alias dev).
              'DEV_PREVIEW=$(curl -fsS -H "Authorization: Bearer $VERCEL_TOKEN" "https://api.vercel.com/v6/deployments?projectId=$VERCEL_PROJECT_ID&teamId=$VERCEL_ORG_ID&target=preview&state=READY&limit=1&meta-githubCommitSha=$GITHUB_SHA" | jq -r ".deployments[0].url")',
              'if [ -z "$DEV_PREVIEW" ] || [ "$DEV_PREVIEW" = "null" ]; then echo "no Preview deploy found for commit $GITHUB_SHA"; exit 1; fi',
              'echo "Re-aliasing dev.goosetown.isol8.co to https://$DEV_PREVIEW (commit $GITHUB_SHA)"',
              'vercel alias "https://$DEV_PREVIEW" dev.goosetown.isol8.co --token=$VERCEL_TOKEN',
            ].join("\n"),
            env: vercelEnv,
          },
          {
            // Apex alias runs LAST so dev correctness above doesn't depend
            // on this step succeeding. The apex alias has been known to
            // fail intermittently on ACM cert issuance — see
            // docs/superpowers/test-runs/2026-05-06-prod-restoration-summary.md.
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
