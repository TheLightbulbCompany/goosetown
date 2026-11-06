import * as cdk from "aws-cdk-lib";
import { GooseTownStage } from "../lib/goosetown-stage";

const context = {
  "availability-zones:account=877352799272:region=us-east-1": ["us-east-1a", "us-east-1b", "us-east-1c"],
  "hosted-zone:account=877352799272:domainName=isol8.co:region=us-east-1": {
    Id: "/hostedzone/Z09248243AOUC775CDUI4", Name: "isol8.co.",
  },
};

test.each(["dev", "prod"])("%s keeps explicit existing CloudFormation stack names", (env) => {
  const app = new cdk.App({ context });
  const stage = new GooseTownStage(app, env, {
    environment: env, env: { account: "877352799272", region: "us-east-1" },
  });
  const actual = stage.node.findAll().filter((node): node is cdk.Stack => node instanceof cdk.Stack)
    .map((stack) => stack.stackName).sort();
  const names = ["api", "auth", "cluster", "compute", "database", "dns", "eventing", "gameserver", "network", "observability", "sprite-worker", "storage", "worldgm"];
  expect(actual).toEqual(names.map((name) => `goosetown-${env}-${name}`).sort());
});
