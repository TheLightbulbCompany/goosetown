/**
 * storage-stack.test.ts
 *
 * Phase 6 hardening: the sprite bucket + CloudFront response policy used to
 * allow ``Access-Control-Allow-Origin: *`` so anyone could hot-link sprites
 * or scrape the bucket via the CDN. Lock the allow-list to known frontend
 * origins per environment, with localhost retained for the Vite dev server.
 */

import * as cdk from "aws-cdk-lib";
import { Template, Match } from "aws-cdk-lib/assertions";
import { StorageStack } from "../lib/stacks/storage-stack";

function buildTestApp(env: string): Template {
  const app = new cdk.App();
  const stack = new StorageStack(app, `goosetown-${env}-storage`, {
    stackName: `goosetown-${env}-storage`,
    environment: env,
    env: { account: "123456789012", region: "us-east-1" },
    // No certificate / hostedZone — both optional. Distribution still
    // synthesizes; just without the custom domain.
  });
  return Template.fromStack(stack);
}

describe("StorageStack — sprite bucket CORS allow-list", () => {
  test("dev bucket allows the dev frontend origin (not '*')", () => {
    const template = buildTestApp("dev");
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: "goosetown-dev-sprites",
      CorsConfiguration: Match.objectLike({
        CorsRules: Match.arrayWith([
          Match.objectLike({
            AllowedOrigins: Match.arrayWith([
              "https://dev.goosetown.isol8.co",
              "http://localhost:5173",
            ]),
          }),
        ]),
      }),
    });
  });

  test("prod bucket allows the prod frontend origin (not '*')", () => {
    const template = buildTestApp("prod");
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: "goosetown-prod-sprites",
      CorsConfiguration: Match.objectLike({
        CorsRules: Match.arrayWith([
          Match.objectLike({
            AllowedOrigins: Match.arrayWith([
              "https://goosetown.isol8.co",
              "http://localhost:5173",
            ]),
          }),
        ]),
      }),
    });
  });

  test("dev bucket does NOT allow wildcard origin", () => {
    const template = buildTestApp("dev");
    const json = JSON.stringify(template.toJSON());
    // The wildcard had been the only entry; assert it's gone from the
    // sprite bucket's CORS rules. Other resources in the template (eg.
    // CloudFront cache-policy comments) shouldn't include the literal
    // ``"*"`` in this position either.
    const buckets = template.findResources("AWS::S3::Bucket");
    const sprite = Object.values(buckets).find(
      (b: any) =>
        b?.Properties?.BucketName === "goosetown-dev-sprites"
    ) as any;
    const rules = sprite?.Properties?.CorsConfiguration?.CorsRules ?? [];
    for (const r of rules) {
      expect(r.AllowedOrigins).not.toContain("*");
    }
    // Sanity: the prod hostname must NOT be allowed by the dev bucket
    // (no cross-env leakage).
    expect(json).not.toMatch(
      /goosetown-dev-sprites[\s\S]*"https:\/\/goosetown\.isol8\.co"/
    );
  });

  test("prod bucket does NOT allow the dev frontend origin", () => {
    const template = buildTestApp("prod");
    const buckets = template.findResources("AWS::S3::Bucket");
    const sprite = Object.values(buckets).find(
      (b: any) =>
        b?.Properties?.BucketName === "goosetown-prod-sprites"
    ) as any;
    const rules = sprite?.Properties?.CorsConfiguration?.CorsRules ?? [];
    for (const r of rules) {
      expect(r.AllowedOrigins).not.toContain("https://dev.goosetown.isol8.co");
      expect(r.AllowedOrigins).not.toContain("*");
    }
  });
});

describe("StorageStack — CloudFront ResponseHeadersPolicy CORS", () => {
  test("dev CDN response policy allows only dev frontend + localhost", () => {
    const template = buildTestApp("dev");
    template.hasResourceProperties(
      "AWS::CloudFront::ResponseHeadersPolicy",
      {
        ResponseHeadersPolicyConfig: Match.objectLike({
          CorsConfig: Match.objectLike({
            AccessControlAllowOrigins: Match.objectLike({
              Items: Match.arrayWith([
                "https://dev.goosetown.isol8.co",
                "http://localhost:5173",
              ]),
            }),
          }),
        }),
      }
    );
  });

  test("prod CDN response policy allows only prod frontend + localhost", () => {
    const template = buildTestApp("prod");
    template.hasResourceProperties(
      "AWS::CloudFront::ResponseHeadersPolicy",
      {
        ResponseHeadersPolicyConfig: Match.objectLike({
          CorsConfig: Match.objectLike({
            AccessControlAllowOrigins: Match.objectLike({
              Items: Match.arrayWith([
                "https://goosetown.isol8.co",
                "http://localhost:5173",
              ]),
            }),
          }),
        }),
      }
    );
  });

  test("CDN response policy does NOT allow wildcard origin", () => {
    const template = buildTestApp("dev");
    const policies = template.findResources(
      "AWS::CloudFront::ResponseHeadersPolicy"
    );
    for (const policy of Object.values(policies) as any[]) {
      const origins =
        policy?.Properties?.ResponseHeadersPolicyConfig?.CorsConfig
          ?.AccessControlAllowOrigins?.Items ?? [];
      expect(origins).not.toContain("*");
    }
  });

  test("CORP and CORS originOverride still applied (regression guard)", () => {
    // Ensure the lockdown didn't accidentally drop the CORP header that
    // COEP:require-corp embedding depends on, and that originOverride
    // remains true so cached-without-CORS responses are still re-headered.
    const template = buildTestApp("dev");
    template.hasResourceProperties(
      "AWS::CloudFront::ResponseHeadersPolicy",
      {
        ResponseHeadersPolicyConfig: Match.objectLike({
          CustomHeadersConfig: Match.objectLike({
            Items: Match.arrayWith([
              Match.objectLike({
                Header: "Cross-Origin-Resource-Policy",
                Value: "cross-origin",
                Override: true,
              }),
            ]),
          }),
          CorsConfig: Match.objectLike({
            OriginOverride: true,
          }),
        }),
      }
    );
  });
});
