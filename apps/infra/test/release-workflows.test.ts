import * as fs from 'fs';
import * as path from 'path';
// js-yaml is already installed via the checked-in infra lockfile.
const yaml = require('js-yaml');
const root = path.resolve(__dirname, '../../..');
const read = (name: string): any => yaml.load(fs.readFileSync(path.join(root, '.github/workflows', name), 'utf8'));

test('manual deployments have independent stage targets and guarded dependency graphs', () => {
  const dev = read('deploy.yml');
  const prod = read('deploy-prod.yml');
  for (const workflow of [dev, prod]) {
    expect(Object.keys(workflow.on)).toEqual(['workflow_dispatch']);
    const jobs = workflow.jobs;
    expect(jobs['Build-Synth'].if).toContain("github.ref == 'refs/heads/main'");
    const guarded = (name: string, seen: Set<string> = new Set()): boolean => {
      if (name === 'Build-Synth') return true;
      if (seen.has(name)) return false;
      const needs = jobs[name]?.needs || [];
      const parents = Array.isArray(needs) ? needs : [needs];
      return parents.length > 0 && parents.every((n: string) => guarded(n, new Set([...seen, name])));
    };
    for (const name of Object.keys(jobs)) expect(guarded(name)).toBe(true);
  }
  expect(prod.jobs['Build-Synth'].environment).toBe('Production');
  expect(prod.jobs['prod-DeployVercelProd'].environment).toBe('Production');
  const prodDeploys = Object.entries(prod.jobs).filter(([name]) => name.startsWith('prod-') && name.endsWith('-Deploy'));
  expect(prodDeploys.length).toBeGreaterThan(0);
  for (const [, job] of prodDeploys) expect((job as any).environment).toBe('Production');
  expect(Object.keys(dev.jobs).some(name => name.startsWith('prod-'))).toBe(false);
  expect(Object.keys(prod.jobs).some(name => name.startsWith('dev-'))).toBe(false);
  expect(JSON.stringify(dev)).not.toContain('goosetown-prod-');
  expect(JSON.stringify(prod)).not.toContain('goosetown-dev-');
  const devText = JSON.stringify(dev);
  const prodText = JSON.stringify(prod);
  expect(devText).toContain('https://api-dev.goosetown.thelightbulb.company/api/v1');
  expect(devText).toContain('wss://ws-dev.goosetown.thelightbulb.company');
  expect(devText).toContain('https://assets-dev.goosetown.thelightbulb.company/_corp-check.png');
  expect(devText).toContain('dev.goosetown.thelightbulb.company');
  expect(prodText).toContain('https://api.goosetown.thelightbulb.company/api/v1');
  expect(prodText).toContain('wss://ws.goosetown.thelightbulb.company');
  expect(prodText).toContain('goosetown.thelightbulb.company');
  expect(devText).not.toContain('goosetown.isol8.co');
  expect(prodText).not.toContain('goosetown.isol8.co');
  const ci = fs.readFileSync(path.join(root, '.github/workflows/ci.yml'), 'utf8');
  expect(ci).toContain('E2E_BASE_URL: https://dev.goosetown.thelightbulb.company');
  expect(ci).not.toContain('goosetown.isol8.co');
});
