import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';
import test from 'node:test';

const workflowDirectoryUrl = new URL('../.github/workflows/', import.meta.url);
const contractUrl = new URL('../deployment/deployment-contract.json', import.meta.url);

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
