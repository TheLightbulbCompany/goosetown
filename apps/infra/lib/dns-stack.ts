import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as route53 from "aws-cdk-lib/aws-route53";
import * as targets from "aws-cdk-lib/aws-route53-targets";
import * as elbv2 from "aws-cdk-lib/aws-elasticloadbalancingv2";
import * as cloudfront from "aws-cdk-lib/aws-cloudfront";
import { Construct } from "constructs";

export interface DnsStackProps extends cdk.StackProps {
  environment: string;
  alb?: elbv2.IApplicationLoadBalancer;
  distribution?: cloudfront.IDistribution;
}

export class DnsStack extends cdk.Stack {
  public readonly certificate: acm.ICertificate;
  public readonly hostedZone: route53.IHostedZone;

  constructor(scope: Construct, id: string, props: DnsStackProps) {
    super(scope, id, props);

    const env = props.environment;

    // Look up existing hosted zone for isol8.co
    this.hostedZone = route53.HostedZone.fromLookup(this, "HostedZone", {
      domainName: "isol8.co",
    });

    // ACM certificate for *.goosetown.isol8.co
    this.certificate = new acm.Certificate(this, "Certificate", {
      domainName: `*.goosetown.isol8.co`,
      subjectAlternativeNames: ["goosetown.isol8.co"],
      validation: acm.CertificateValidation.fromDns(this.hostedZone),
    });

    // A record: api-{env}.goosetown.isol8.co -> ALB
    if (props.alb) {
      new route53.ARecord(this, "ApiRecord", {
        zone: this.hostedZone,
        recordName: `api-${env}.goosetown.isol8.co`,
        target: route53.RecordTarget.fromAlias(
          new targets.LoadBalancerTarget(props.alb)
        ),
      });
    }

    // A record: assets-{env}.goosetown.isol8.co -> CloudFront
    if (props.distribution) {
      new route53.ARecord(this, "AssetsRecord", {
        zone: this.hostedZone,
        recordName: `assets-${env}.goosetown.isol8.co`,
        target: route53.RecordTarget.fromAlias(
          new targets.CloudFrontTarget(props.distribution)
        ),
      });
    }

    // Outputs
    new cdk.CfnOutput(this, "CertificateArn", {
      value: this.certificate.certificateArn,
      exportName: `goosetown-${env}-certificate-arn`,
    });
  }
}
