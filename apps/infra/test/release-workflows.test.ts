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
