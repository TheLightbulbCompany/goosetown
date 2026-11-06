import * as cdk from "aws-cdk-lib";
import * as acm from "aws-cdk-lib/aws-certificatemanager";
import { Match, Template } from "aws-cdk-lib/assertions";
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
