import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import test from 'node:test';

const workflowDirectoryUrl = new URL('../.github/workflows/', import.meta.url);
const contractUrl = new URL('../deployment/deployment-contract.json', import.meta.url);
const composeUrl = new URL('../compose.yaml', import.meta.url);

async function deploymentWorkflow() {
  const names = (await readdir(workflowDirectoryUrl)).filter((name) => /\.ya?ml$/.test(name));
  const candidates = [];
  for (const name of names) {
    const body = await readFile(new URL(name, workflowDirectoryUrl), 'utf8');
    if (body.includes('Resolve exact registry digest') && body.includes('Deploy, poll, verify, and roll back on failure')) {
      candidates.push({ name, body });
    }
  }
  assert.equal(candidates.length, 1, 'expected exactly one immutable Coolify deployment workflow');
  return candidates[0];
}

test('production and staging workflows are independent', async () => {
  const { body: workflow } = await deploymentWorkflow();
  for (const pattern of [
    /Require a green staging run/i,
    /No successful staging run/i,
    /Waiting for staging run/i,
    /actions\/runs\?branch=staging[^\n]*head_sha=/i,
  ]) {
    assert.doesNotMatch(workflow, pattern);
  }
  assert.match(workflow, /branches:\s*\[main, staging\]/);
  assert.match(workflow, /group:\s*[^\n]*\$\{\{ github\.ref_name \}\}/);
  assert.match(workflow, /test \"\$\{\{ github\.repository \}\}\"/);
  assert.match(workflow, /ref:\s*\$\{\{ github\.sha \}\}/);
  assert.match(workflow, /needs\.deploy(?:_frontend|_backend)?\.result != 'skipped'/);
  assert.doesNotMatch(workflow, /runs-on:\s*\[?self-hosted/i);
  assert.equal([...workflow.matchAll(/^\s*runs-on:\s*ubuntu-latest\s*$/gm)].length, 4);
});

test('deployment contract declares independent environment execution', async () => {
  const contract = JSON.parse(await readFile(contractUrl, 'utf8'));
  assert.deepEqual(contract.environment_execution, {
    mode: 'independent',
    cross_environment_wait: false,
    cross_environment_failure_coupling: false,
  });
  assert.equal('promotion' in contract, false);
  assert.equal(contract.runner, 'ubuntu-latest');
});

test('deployment safety controls remain present', async () => {
  const { body: workflow } = await deploymentWorkflow();
  for (const marker of [
    'Resolve exact registry digest',
    'Verify target-scoped deployment variables',
    'Fetch target-scoped Coolify credentials from Infisical',
    'Deploy, poll, verify, and roll back on failure',
    'Independently clear temporary GHCR credentials',
  ]) {
    assert.match(workflow, new RegExp(marker.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')));
  }
});

test('Coolify-generated proxy configuration is the sole Traefik route owner', async () => {
  const compose = await readFile(composeUrl, 'utf8');
  assert.doesNotMatch(compose, /^\s*labels:\s*$/m);
  assert.doesNotMatch(compose, /traefik\./i);
});

test('direct Coolify redeploy reuses only an approved cached digest', async () => {
  const body = await readFile(new URL('../scripts/coolify_ghcr_start.sh', import.meta.url), 'utf8');
  assert.match(body, /image_is_approved_locally/);
  assert.match(body, /Using the cached approved image/);
  assert.match(body, /Approved image is not cached and no usable GHCR pull credential is available/);
  assert.match(body, /RELEASE_PULL_POLICY=never/);
});
