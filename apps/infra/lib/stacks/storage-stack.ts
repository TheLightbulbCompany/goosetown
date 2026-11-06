import { canonicalHost, frontendOrigin, canonicalZoneId, canonicalZoneName } from "./canonical-domain";
import * as cdk from "aws-cdk-lib";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as route53Targets from "aws-cdk-lib/aws-route53-targets";
import * as wafv2 from "aws-cdk-lib/aws-wafv2";
import { Construct } from "constructs";

export interface StorageStackProps extends cdk.StackProps {
  environment: string;
  certificate?: acm.ICertificate;
  hostedZone?: route53.IHostedZone;
}

export class StorageStack extends cdk.Stack {
  public readonly spriteBucket: s3.Bucket;
  public readonly distribution: cloudfront.Distribution;

  constructor(scope: Construct, id: string, props: StorageStackProps) {
    super(scope, id, props);

    const env = props.environment;

    // Allowed origins for sprite fetches. Frontends + local dev only — no
    // wildcard. Anyone hot-linking sprites from outside this list gets a
    // CORS error at the edge. Per-env so dev frontend isn't allowed against
    // prod bucket and vice versa; localhost stays in the list everywhere
    // because the frontend dev server uses it for both envs.
    const allowedOrigins =
      env === "prod"
        ? [frontendOrigin(env), "http://localhost:5173"]
        : [frontendOrigin(env), "http://localhost:5173"];

    // S3 bucket for sprites.
    //
    // ``versioned: true`` gives us object-level recovery for accidental
    // deletes/overwrites. Sprite objects are tiny (~5–15 KB PNG) and there
    // are at most ~1 per agent + a handful of animation variants, so the
    // versioning storage cost is negligible. NOTE: this is *prospective*
    // recovery — only writes after the change deploys get version history;
    // existing objects do not pick up versions retroactively.
    //
    // ``noncurrentVersionExpiration: 30 days`` keeps the version pile from
    // growing forever. Any accidental delete that goes unnoticed for a
    // month probably wasn't worth recovering, and PixelLab can regenerate
    // sprites if it ever comes to that.
    this.spriteBucket = new s3.Bucket(this, "SpriteBucket", {
      bucketName: `goosetown-${env}-sprites`,
      removalPolicy:
        env === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: env !== "prod",
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      versioned: true,
      lifecycleRules: [
        {
          id: "expire-noncurrent-sprite-versions",
          noncurrentVersionExpiration: cdk.Duration.days(30),
        },
        // Exchange job artifacts (W5): provider-uploaded deliverables live
        // under artifacts/{job_id}/ and are only reachable via presigned
        // URLs (24 h GET). The consumer daemon downloads them at delivery
        // time, so 30 days is generous retention before S3 reaps them.
        {
          id: "expire-exchange-artifacts",
          prefix: "artifacts/",
          expiration: cdk.Duration.days(30),
        },
      ],
      cors: [
        {
          allowedMethods: [s3.HttpMethods.GET, s3.HttpMethods.HEAD],
          allowedOrigins,
          allowedHeaders: ["*"],
          maxAge: 86400,
        },
      ],
    });

    // ResponseHeadersPolicy: authoritative CORS + CORP headers applied at
    // the edge on EVERY response (including cached ones), so sprites can be
    // loaded by the frontend, which serves Cross-Origin-Embedder-Policy:
    // require-corp (needed for Godot WebGL SharedArrayBuffer).
    //
    // Two headers need to be present for a sprite fetch to succeed from the
    // COEP page:
    //   * Cross-Origin-Resource-Policy — COEP:require-corp will block cross-
    //     origin images without this.
    //   * Access-Control-Allow-Origin — Chrome's `fetch(url)` (which the
    //     Godot JS bridge uses to decode PNG bytes into a WebGL texture) fails
    //     without CORS even on HTTP 200.
    //
    // Before this change only CORP was set via customHeadersBehavior; ACAO
    // came from S3 per-object CORS metadata, which meant responses cached at
    // the edge BEFORE the S3 CORS config was applied lacked ACAO entirely.
    // CloudFront served those cached-without-ACAO responses for a full year
    // (max-age=31536000), breaking sprite loads for affected files (e.g.
    // Pam's walk.png cached on 2026-04-20, broke after the policy change).
    //
    // `corsBehavior` with `originOverride: true` makes CloudFront add the
    // CORS headers on every response regardless of what's in the cached
    // object, which eliminates the "stale cache serves no-CORS response"
    // failure mode.
    const responseHeadersPolicy = new cloudfront.ResponseHeadersPolicy(
      this,
      "SpriteResponseHeaders",
      {
        responseHeadersPolicyName: `goosetown-${env}-sprite-headers`,
        comment: "CORS + CORP for COEP:require-corp embedding",
        corsBehavior: {
          accessControlAllowOrigins: allowedOrigins,
          accessControlAllowMethods: ["GET", "HEAD"],
          accessControlAllowHeaders: ["*"],
          accessControlAllowCredentials: false,
          accessControlMaxAge: cdk.Duration.seconds(86400),
          originOverride: true,
        },
        customHeadersBehavior: {
          customHeaders: [
            {
              header: "Cross-Origin-Resource-Policy",
              value: "cross-origin",
              override: true,
            },
          ],
        },
      }
    );

    // ─── WAF (CLOUDFRONT scope, attached to the distribution) ─────────────
    // CloudFront WebACLs MUST live in us-east-1 (which this account already
    // deploys to) and MUST be passed to the distribution at create time —
    // unlike ALB, there's no separate ``CfnWebACLAssociation`` for
    // CloudFront. Same rule layout as the ALB ACL (see compute-stack.ts):
    // IP reputation + KnownBadInputs in BLOCK, CommonRuleSet in BLOCK
    // (promoted from COUNT after >7 days FP review, PR #216), rate-based 2000 / 5min / IP.
    // Sprite traffic is much lower-volume than the API; the rate limit
    // here primarily protects against scraping the bucket via the CDN.
    const cdnWaf = new wafv2.CfnWebACL(this, "CdnWebAcl", {
      name: `goosetown-${env}-cdn-waf`,
      scope: "CLOUDFRONT",
      defaultAction: { allow: {} },
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: `goosetown-${env}-cdn-waf`,
        sampledRequestsEnabled: true,
      },
      rules: [
        {
          name: "AWSManagedRulesAmazonIpReputationList",
          priority: 0,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: "AWS",
              name: "AWSManagedRulesAmazonIpReputationList",
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "ip-reputation",
            sampledRequestsEnabled: true,
          },
        },
        {
          name: "AWSManagedRulesKnownBadInputsRuleSet",
          priority: 1,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: "AWS",
              name: "AWSManagedRulesKnownBadInputsRuleSet",
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "known-bad-inputs",
            sampledRequestsEnabled: true,
          },
        },
        {
          name: "AWSManagedRulesCommonRuleSet",
          priority: 2,
          // BLOCK mode after >7 days FP review (PR #216). Mirrors the ALB
          // WebACL posture — see compute-stack.ts for full rationale.
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: "AWS",
              name: "AWSManagedRulesCommonRuleSet",
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "common-rules",
            sampledRequestsEnabled: true,
          },
        },
        {
          name: "RateLimit2000Per5Min",
          priority: 3,
          action: { block: {} },
          statement: {
            rateBasedStatement: {
              limit: 2000,
              aggregateKeyType: "IP",
              evaluationWindowSec: 300,
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: "rate-limit",
            sampledRequestsEnabled: true,
          },
        },
      ],
    });

    // CloudFront distribution with S3 bucket origin (OAC managed automatically)
    const domainName = canonicalHost("assets", env);
    this.distribution = new cloudfront.Distribution(this, "Distribution", {
      comment: `GooseTown ${env} sprites`,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(this.spriteBucket),
        viewerProtocolPolicy:
          cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        responseHeadersPolicy,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
      },
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      webAclId: cdnWaf.attrArn,
      ...(props.certificate && {
        domainNames: [domainName],
        certificate: props.certificate,
      }),
    });

    const canonicalZone = route53.HostedZone.fromHostedZoneAttributes(this, "CanonicalZone", {
      hostedZoneId: canonicalZoneId, zoneName: canonicalZoneName,
    });
    new route53.ARecord(this, "CanonicalAssetsDnsRecord", {
      zone: canonicalZone, recordName: canonicalHost("assets", env),
      target: route53.RecordTarget.fromAlias(new route53Targets.CloudFrontTarget(this.distribution)),
    });

    // Outputs
    new cdk.CfnOutput(this, "SpriteBucketName", {
      value: this.spriteBucket.bucketName,
    });
    new cdk.CfnOutput(this, "DistributionDomainName", {
      value: this.distribution.distributionDomainName,
    });
    new cdk.CfnOutput(this, "DistributionId", {
      value: this.distribution.distributionId,
    });
  }
}
