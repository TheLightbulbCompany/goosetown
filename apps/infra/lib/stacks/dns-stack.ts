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

    // Outputs
    new cdk.CfnOutput(this, "CertificateArn", {
      value: this.certificate.certificateArn,
      exportName: `goosetown-${env}-certificate-arn`,
    });
  }
}
