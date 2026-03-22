import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import { GitHubActionRole } from "cdk-pipelines-github";

export class GitHubActionRoleStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    new GitHubActionRole(this, "GooseTownGitHubRole", {
      repos: ["Isol8AI/goosetown"],
      roleName: "goosetown-github-actions",
      provider: GitHubActionRole.existingGitHubActionsProvider(this),
    });
  }
}
