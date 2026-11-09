import * as fs from "fs";
import * as path from "path";
const yaml = require("js-yaml");
const root = path.resolve(__dirname, "../../..");
const read = (name: string): any => yaml.load(fs.readFileSync(path.join(root, ".github/workflows", name), "utf8"));

test("manual workflows are hard-disabled and production credentials require its environment", () => {
  const dev = read("deploy.yml");
  const prod = read("deploy-prod.yml");
  for (const workflow of [dev, prod]) {
    expect(Object.keys(workflow.on)).toEqual(["workflow_dispatch"]);
    expect(Object.keys(workflow.jobs)).toEqual(["deploy"]);
    expect(workflow.jobs.deploy.if).toBe("${{ false }}");
  }
  expect(prod.jobs.deploy.environment).toBe("Production");
  expect(prod.jobs.deploy.permissions["id-token"]).toBe("write");
  expect(JSON.stringify(prod.jobs.deploy.steps)).toContain("prod/*");
  expect(JSON.stringify(dev)).not.toContain("id-token");
  expect(JSON.stringify(dev)).not.toContain("goosetown-github-actions");
});


test("parking door is manual, scoped, and cannot enable the broad deploy", () => {
  const w=read("park-legacy-world.yml");
  expect(Object.keys(w.on)).toEqual(["workflow_dispatch"]);
  expect(w.on.workflow_dispatch.inputs.mode.options).toEqual(["plan","execute"]);
  expect(w.jobs.park.environment).toBe("Production");
  const credential=w.jobs.park.steps.find((s:any)=>s.uses?.startsWith("aws-actions/configure-aws-credentials"));
  const policy=JSON.parse(credential.with["inline-session-policy"]);
  const writes=policy.Statement[0];
  expect(writes.Action).not.toContain("cloudformation:DeleteStack");
  expect(writes.Action).not.toContain("cloudformation:UpdateStack");
  expect(writes.Resource.every((r:string)=>r.includes("goosetown-${{ inputs.environment }}-") || r.includes("changeSet/goose-park-"))).toBe(true);
  expect(JSON.stringify(policy)).not.toContain("lightbulb-prod");
  expect(read("deploy.yml").jobs.deploy.if).toBe("${{ false }}");
  expect(read("deploy-prod.yml").jobs.deploy.if).toBe("${{ false }}");
});

test("edge retirement is main-only with separate reviewed phases and exact DNS delete authority", () => {
  const workflow = read("retire-legacy-edge.yml");
  expect(Object.keys(workflow.on)).toEqual(["workflow_dispatch"]);
  expect(workflow.on.workflow_dispatch.inputs.phase.options).toEqual(["edge-prepare", "dns", "edge"]);
  expect(workflow.on.workflow_dispatch.inputs.mode.default).toBe("plan");
  expect(workflow.jobs.retire.if).toBe("github.ref == 'refs/heads/main'");
  expect(workflow.jobs.retire.environment).toBe("Production");
  const credential = workflow.jobs.retire.steps.find((step: any) => step.uses?.startsWith("aws-actions/configure-aws-credentials"));
  const rendered = credential.with["inline-session-policy"]
    .replace(/\$\{\{ inputs.environment ==.*?\}\}/g, "api-dev.goosetown.thelightbulb.company")
    .replace(/\$\{\{ inputs.environment \}\}/g, "dev");
  expect(rendered.length).toBeLessThanOrEqual(2048);
  const policy = JSON.parse(rendered);
  const dns = policy.Statement.find((statement: any) => statement.Action === "route53:ChangeResourceRecordSets");
  expect(dns.Resource).toBe("arn:aws:route53:::hostedzone/Z09913772LJJ6H4X9JJX0");
  expect(dns.Condition["ForAllValues:StringEquals"]).toEqual({
    "route53:ChangeResourceRecordSetsNormalizedRecordNames": ["api-dev.goosetown.thelightbulb.company"],
    "route53:ChangeResourceRecordSetsRecordTypes": ["A"],
    "route53:ChangeResourceRecordSetsActions": ["DELETE"],
  });
  expect(JSON.stringify(policy.Statement[0].Resource)).toContain("stack/goosetown-dev-compute/");
  expect(JSON.stringify(policy.Statement[0].Resource)).not.toContain("lightbulb-");
  expect(JSON.stringify(policy)).not.toContain("secretsmanager:");
  expect(JSON.stringify(policy)).not.toContain("iam:");
  expect(read("deploy.yml").jobs.deploy.if).toBe("${{ false }}");
  expect(read("deploy-prod.yml").jobs.deploy.if).toBe("${{ false }}");
});
