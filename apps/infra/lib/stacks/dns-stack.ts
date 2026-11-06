import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import * as route53 from "aws-cdk-lib/aws-route53";
import { Construct } from "constructs";
import { canonicalCertificateArn, canonicalZoneId, canonicalZoneName } from "./canonical-domain";

export interface DnsStackProps extends cdk.StackProps { environment: string; }

/** Canonical zone/certificate already exist; review CloudFormation adoption before deployment. */
export class DnsStack extends cdk.Stack {
  public readonly certificate: acm.ICertificate;
  public readonly hostedZone: route53.IHostedZone;
  constructor(scope: Construct, id: string, props: DnsStackProps) {
    super(scope, id, props);
    this.hostedZone = route53.HostedZone.fromHostedZoneAttributes(this, "CanonicalZone", {
      hostedZoneId: canonicalZoneId, zoneName: canonicalZoneName,
    });
    // Keep the old resource and auto-export until deployed importers migrate.
    const legacyZone = route53.HostedZone.fromLookup(this, "HostedZone", { domainName: "isol8.co" });
    const legacyCertificate = new acm.Certificate(this, "Certificate", {
      domainName: "*.goosetown.isol8.co",
      subjectAlternativeNames: ["goosetown.isol8.co"],
      validation: acm.CertificateValidation.fromDns(legacyZone),
    });
    this.exportValue(legacyCertificate.certificateArn);
    this.certificate = acm.Certificate.fromCertificateArn(this, "CanonicalCertificate", canonicalCertificateArn);
    new cdk.CfnOutput(this, "CertificateArn", {
      value: legacyCertificate.certificateArn,
      exportName: `goosetown-${props.environment}-certificate-arn`,
    });
  }
}
