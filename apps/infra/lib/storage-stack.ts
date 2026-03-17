import * as cdk from "aws-cdk-lib";
import * as s3 from "aws-cdk-lib/aws-s3";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import * as origins from "aws-cdk-lib/aws-cloudfront-origins";
// Using S3BucketOrigin instead of deprecated S3Origin
import { Construct } from "constructs";

export interface StorageStackProps extends cdk.StackProps {
  environment: string;
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

    // CloudFront distribution with S3 bucket origin (OAC managed automatically)
    this.distribution = new cloudfront.Distribution(this, "Distribution", {
      comment: `GooseTown ${env} sprites`,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(this.spriteBucket),
        viewerProtocolPolicy:
          cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
      },
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
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
