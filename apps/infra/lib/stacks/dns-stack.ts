import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as route53 from "aws-cdk-lib/aws-route53";
import { Construct } from "constructs";

export interface DnsStackProps extends cdk.StackProps {
  environment: string;
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

    // Vercel apex A record for the prod frontend.
    //
    // Without this, the `vercel alias … goosetown.isol8.co` step in deploy.yml
    // fails at cert issuance because Vercel cannot prove ownership of the host
    // (the hosted zone is on Route53/AWS, not Vercel-DNS). 76.76.21.21 is
    // Vercel's documented edge IP for apex A records — same value used for the
    // dev subdomain (created out-of-band; not managed here).
    //
    // Only created in prod: dev uses dev.goosetown.isol8.co (record exists
    // outside CDK; leaving it alone to avoid an unmanaged-vs-managed conflict).
    if (env === "prod") {
      new route53.ARecord(this, "VercelApexRecord", {
        zone: this.hostedZone,
        recordName: "goosetown.isol8.co",
        target: route53.RecordTarget.fromIpAddresses("76.76.21.21"),
        ttl: cdk.Duration.minutes(5),
        comment: "Vercel apex for prod frontend (goosetown.isol8.co)",
      });
    }

    // Outputs
    new cdk.CfnOutput(this, "CertificateArn", {
      value: this.certificate.certificateArn,
      exportName: `goosetown-${env}-certificate-arn`,
    });
  }
}
