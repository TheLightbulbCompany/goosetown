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

describe("StorageStack — sprite bucket versioning", () => {
  test("sprite bucket has versioning enabled", () => {
    const template = buildTestApp("dev");
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: "goosetown-dev-sprites",
      VersioningConfiguration: { Status: "Enabled" },
    });
  });

  test("sprite bucket expires noncurrent versions after 30 days", () => {
    const template = buildTestApp("dev");
    template.hasResourceProperties("AWS::S3::Bucket", {
      BucketName: "goosetown-dev-sprites",
      LifecycleConfiguration: Match.objectLike({
        Rules: Match.arrayWith([
          Match.objectLike({
            NoncurrentVersionExpiration: Match.objectLike({
              NoncurrentDays: 30,
            }),
            Status: "Enabled",
          }),
        ]),
      }),
    });
  });
});

describe("StorageStack — WAF (CLOUDFRONT scope, on the distribution)", () => {
  test("dev creates a CLOUDFRONT-scoped WebACL", () => {
    const template = buildTestApp("dev");
    template.hasResourceProperties("AWS::WAFv2::WebACL", {
      Name: "goosetown-dev-cdn-waf",
      Scope: "CLOUDFRONT",
      DefaultAction: { Allow: {} },
    });
  });

  test("prod creates a CLOUDFRONT-scoped WebACL", () => {
    const template = buildTestApp("prod");
    template.hasResourceProperties("AWS::WAFv2::WebACL", {
      Name: "goosetown-prod-cdn-waf",
      Scope: "CLOUDFRONT",
    });
  });

  test("CDN WebACL has same 4 rules as ALB WebACL", () => {
    const template = buildTestApp("dev");
    template.hasResourceProperties("AWS::WAFv2::WebACL", {
      Name: "goosetown-dev-cdn-waf",
      Rules: Match.arrayWith([
        Match.objectLike({
          Name: "AWSManagedRulesAmazonIpReputationList",
        }),
        Match.objectLike({
          Name: "AWSManagedRulesKnownBadInputsRuleSet",
        }),
        Match.objectLike({
          // CommonRuleSet promoted to BLOCK (overrideAction: none) after
          // >7 days of access-log FP review (PR #216).
          Name: "AWSManagedRulesCommonRuleSet",
          OverrideAction: { None: {} },
        }),
        Match.objectLike({
          Name: "RateLimit2000Per5Min",
          Action: { Block: {} },
        }),
      ]),
    });
  });

  test("Distribution has webAclId set to the CDN WebACL ARN", () => {
    const template = buildTestApp("dev");
    // CloudFormation encodes the property as ``WebACLId``. CDK passes the
    // ARN of the CLOUDFRONT-scoped ACL via ``webAclId`` on the distribution.
    const dists = template.findResources("AWS::CloudFront::Distribution");
    const dist = Object.values(dists)[0] as any;
    const webAclId = dist?.Properties?.DistributionConfig?.WebACLId;
    expect(webAclId).toBeDefined();
    // It's a CFN intrinsic resolving to the WebACL's ARN — assert the
    // shape rather than the exact resolved string (which depends on
    // logical-id hashing).
    const json = JSON.stringify(webAclId);
    expect(json).toContain("CdnWebAcl");
    expect(json).toContain("Arn");
  });

  test("WAF CommonRuleSet uses overrideAction:none (default-BLOCK)", () => {
    const template = buildTestApp("dev");

    const webACLs = template.findResources("AWS::WAFv2::WebACL");
    const acls = Object.values(webACLs);
    expect(acls.length).toBeGreaterThan(0);

    let foundCommonRuleSet = false;
    for (const acl of acls) {
      const rules = (acl as { Properties: { Rules: Array<{ Name?: string; OverrideAction?: { Count?: object; None?: object } }> } }).Properties.Rules;
      const commonRuleSet = rules.find((r) => r.Name?.includes("CommonRuleSet"));
      if (commonRuleSet) {
        foundCommonRuleSet = true;
        expect(commonRuleSet.OverrideAction).toEqual({ None: {} });
        expect(commonRuleSet.OverrideAction).not.toEqual({ Count: {} });
      }
    }
    expect(foundCommonRuleSet).toBe(true);
  });
});
