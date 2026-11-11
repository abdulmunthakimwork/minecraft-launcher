const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/bridge.js', 'utf8');

function method(name) {
  const expression = source.match(new RegExp(name + ': async \\(\\) => ([^\\n]+),'));
  assert.ok(expression, 'host method exists: ' + name);
  const context = { inertResult: {ok: true}, hostError: (code, message) => Object.assign(new Error(message), {code}) };
  vm.createContext(context);
  return vm.runInContext('(async () => ' + expression[1] + ')', context);
}

for (const name of ['purchaseStoreProduct', 'respondToRealmsInvite', 'leaveRealm', 'redeemRealmsSubscription']) {
  test(name + ' reports unavailable instead of acknowledging an unsupported operation', async () => {
    const invoke = method(name);
    await assert.rejects(() => invoke(), /unavailable in this build/i);
  });
}

test('local Java capability does not imply ownership or support other products', () => {
  const begin = source.indexOf('  const canUseLocalJava =');
  const end = source.indexOf('  const refreshAccounts =', begin);
  assert.ok(begin >= 0 && end > begin, 'explicit local capability helper exists');
  const offline = {offline: true, entitlements: [], entitlementsSource: {}};
  const seed = {capabilities: {localJava: true, realms: false, purchases: false}};
  const context = {seed, activeRecord: () => offline};
  vm.createContext(context);
  vm.runInContext(source.slice(begin, end) + '\nglobalThis.canUse = canUseLocalJava;', context);
  assert.equal(context.canUse('java'), true);
  assert.equal(context.canUse('bedrock'), false);
  assert.equal(context.canUse('java', {offline: false}), false);
  assert.equal(context.canUse('java', null), false);
  assert.deepEqual(offline.entitlements, []);
  seed.capabilities.localJava = false;
  assert.equal(context.canUse('java'), false);
});
