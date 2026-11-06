import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import { Match, Template } from "aws-cdk-lib/assertions";
import { DnsStack } from "../lib/stacks/dns-stack";
import { StorageStack } from "../lib/stacks/storage-stack";
import { canonicalHost, frontendOrigin } from "../lib/stacks/canonical-domain";

test("canonical names distinguish production and development", () => {
  expect(canonicalHost("api", "prod")).toBe("api.goosetown.thelightbulb.company");
  expect(canonicalHost("ws", "dev")).toBe("ws-dev.goosetown.thelightbulb.company");
  expect(frontendOrigin("prod")).toBe("https://goosetown.thelightbulb.company");
});

test.each(["dev", "prod"])("%s sprite distribution uses only the canonical alias", (env) => {
  const app = new cdk.App();
  const scope = new cdk.Stack(app, `cert-${env}`);
  const certificate = acm.Certificate.fromCertificateArn(scope, "Cert", "arn:aws:acm:us-east-1:123456789012:certificate/test");
  const stack = new StorageStack(app, `storage-${env}`, {
    environment: env, certificate, env: { account: "123456789012", region: "us-east-1" },
  });
  const template = Template.fromStack(stack);
  template.hasResourceProperties("AWS::CloudFront::Distribution", {
    DistributionConfig: Match.objectLike({
      Aliases: [canonicalHost("assets", env)],
    }),
  });
  template.hasResourceProperties("AWS::S3::Bucket", {
    BucketName: `goosetown-${env}-sprites`,
    CorsConfiguration: Match.objectLike({ CorsRules: Match.arrayWith([
      Match.objectLike({ AllowedOrigins: Match.arrayWith([frontendOrigin(env)]) }),
    ]) }),
  });
});


test.each(["dev", "prod"])("%s keeps the legacy certificate logical ID and export without DNS record creation", (env) => {
  const context = {"hosted-zone:account=877352799272:domainName=isol8.co:region=us-east-1": {Id:"/hostedzone/Z09248243AOUC775CDUI4", Name:"isol8.co."}};
  const app = new cdk.App({context});
  const stack = new DnsStack(app, `goosetown-${env}-dns`, {
    stackName: `goosetown-${env}-dns`, environment: env,
    env: {account:"877352799272",region:"us-east-1"},
  });
  const doc = Template.fromStack(stack).toJSON();
  expect(doc.Resources.Certificate4E7ABB08.Type).toBe("AWS::CertificateManager::Certificate");
  expect(doc.Outputs.ExportsOutputRefCertificate4E7ABB08F7C8AF50.Export.Name)
    .toBe(`goosetown-${env}-dns:ExportsOutputRefCertificate4E7ABB08F7C8AF50`);
  expect(Object.values(doc.Resources).some((r: any) => r.Type === "AWS::Route53::RecordSet")).toBe(false);
});
