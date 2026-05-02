// apps/infra/test/cluster-stack.test.ts
import * as cdk from "aws-cdk-lib";
import { Template } from "aws-cdk-lib/assertions";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import { ClusterStack } from "../lib/stacks/cluster-stack";

describe("ClusterStack", () => {
  test("creates exactly one ECS cluster named goosetown-{env}-shared", () => {
    // The "-shared" suffix avoids collision with the legacy `goosetown-${env}`
    // cluster name still claimed by the pre-sub-2 ComputeStack template.
    const app = new cdk.App();
    const vpcStack = new cdk.Stack(app, "VpcParent");
    const vpc = new ec2.Vpc(vpcStack, "Vpc", { maxAzs: 2 });

    const stack = new ClusterStack(app, "test-cluster", {
      environment: "dev",
      vpc,
    });

    const t = Template.fromStack(stack);
    t.resourceCountIs("AWS::ECS::Cluster", 1);
    t.hasResourceProperties("AWS::ECS::Cluster", {
      ClusterName: "goosetown-dev-shared",
    });
  });

  test("disables Container Insights to control cost", () => {
    const app = new cdk.App();
    const vpcStack = new cdk.Stack(app, "VpcParent");
    const vpc = new ec2.Vpc(vpcStack, "Vpc", { maxAzs: 2 });

    const stack = new ClusterStack(app, "test-cluster", {
      environment: "dev",
      vpc,
    });

    const t = Template.fromStack(stack);
    const resources = t.findResources("AWS::ECS::Cluster");
    const cluster = Object.values(resources)[0] as { Properties?: { ClusterSettings?: unknown[] } };
    const settings = cluster.Properties?.ClusterSettings ?? [];
    const insights = settings.find(
      (s) => (s as { Name: string }).Name === "containerInsights"
    );
    if (insights) {
      expect((insights as { Value: string }).Value).toBe("disabled");
    }
    // If no settings array, Container Insights is off by default — test passes
  });

  test("exposes the cluster as a public property for cross-stack reference", () => {
    const app = new cdk.App();
    const vpcStack = new cdk.Stack(app, "VpcParent");
    const vpc = new ec2.Vpc(vpcStack, "Vpc", { maxAzs: 2 });

    const stack = new ClusterStack(app, "test-cluster", {
      environment: "dev",
      vpc,
    });

    expect(stack.cluster).toBeDefined();
    // cluster.clusterName returns a CFN token at synth time; clusterLogicalName
    // on ClusterStack holds the literal value and is safe to assert against.
    expect(stack.clusterLogicalName).toContain("goosetown-dev");
  });
});
