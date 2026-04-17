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
          allowedOrigins: ["*"],
          allowedHeaders: ["*"],
          maxAge: 86400,
        },
      ],
    });

    // ResponseHeadersPolicy: add Cross-Origin-Resource-Policy so sprites can be
    // <img> loaded by the frontend, which serves Cross-Origin-Embedder-Policy:
    // require-corp (needed for Godot WebGL SharedArrayBuffer). Without this
    // header, COEP blocks cross-origin image loads even when HTTP 200s.
    const responseHeadersPolicy = new cloudfront.ResponseHeadersPolicy(
      this,
      "SpriteResponseHeaders",
      {
        responseHeadersPolicyName: `goosetown-${env}-sprite-headers`,
        comment: "Allow embedding in COEP:require-corp pages",
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
