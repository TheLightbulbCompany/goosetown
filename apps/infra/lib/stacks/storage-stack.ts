import * as cdk from "aws-cdk-lib";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as route53Targets from "aws-cdk-lib/aws-route53-targets";
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
        ? ["https://goosetown.isol8.co", "http://localhost:5173"]
        : ["https://dev.goosetown.isol8.co", "http://localhost:5173"];

    // S3 bucket for sprites
    this.spriteBucket = new s3.Bucket(this, "SpriteBucket", {
      bucketName: `goosetown-${env}-sprites`,
      removalPolicy:
        env === "prod" ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: env !== "prod",
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
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

    // CloudFront distribution with S3 bucket origin (OAC managed automatically)
    const domainName = `assets-${env}.goosetown.isol8.co`;
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
      ...(props.certificate && {
        domainNames: [domainName],
        certificate: props.certificate,
      }),
    });

    // DNS record: assets-{env}.goosetown.isol8.co -> CloudFront
    if (props.hostedZone) {
      new route53.ARecord(this, "AssetsDnsRecord", {
        zone: props.hostedZone,
        recordName: domainName,
        target: route53.RecordTarget.fromAlias(
          new route53Targets.CloudFrontTarget(this.distribution)
        ),
      });
    }

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
