const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(__dirname + '/bridge.js', 'utf8');

function contextFor(start, end, exposed, extras = {}) {
  const seed = {launcherSettings: {locale: 'en-US'}, configurationData: {settings: {sort: 'name'}},
    skins: {customSkins: {old: {id: 'old'}}, activeSkin: {id: 'old'}, defaultSkins: {steve: {id: 'steve'}}}};
  const sandbox = {seed, console, pushSlice() {}, hostError: (code, message) => Object.assign(new Error(message), {code}),
    fetch: async () => ({ok: false, json: async () => ({error: 'disk failure'})}),
    osRequest: async () => ({ok: false, error: 'host refused'}),
    common: {setStore: async () => false}, ...extras};
  vm.createContext(sandbox);
  const begin = source.indexOf(start), finish = source.indexOf(end, begin);
  assert.ok(begin >= 0 && finish > begin, 'source block exists');
  vm.runInContext(source.slice(begin, finish) + '\nglobalThis.tested = ' + exposed + ';', sandbox);
  return sandbox;
}

for (const [name, start, end, field] of [
  ['launcher settings', '  const saveLauncherSettings =', '  // --- installations', 'launcherSettings'],
  ['configuration settings', '  const saveConfigurationSettings =', '  // Native helpers', 'configurationData'],
]) {
  test(name + ' serialize rapid saves and recover the queue after a failure', async () => {
    const requests = [];
    const scope = contextFor(start, end, name.startsWith('launcher') ? 'saveLauncherSettings' : 'saveConfigurationSettings', {
      osRequest: (url, patch) => new Promise(resolve => requests.push({url, patch, resolve})),
    });
    const first = scope.tested({sort:'first', locale:'first'});
    const second = scope.tested({sort:'second', locale:'second'});
    await Promise.resolve();
    assert.equal(requests.length, 1, 'the later preference waits for the first acknowledgment');
    requests[0].resolve({ok:true});
    await first; await Promise.resolve();
    assert.equal(requests.length, 2);
    requests[1].resolve({ok:true});
    await second;
    assert.match(JSON.stringify(scope.seed[field]), /second/);
    const refused = scope.tested({sort:'refused'});
    await Promise.resolve(); requests[2].resolve({ok:false,error:'offline'});
    await assert.rejects(refused, /offline/);
    const next = scope.tested({sort:'recovered'});
    await Promise.resolve(); requests[3].resolve({ok:true});
    await next;
    assert.match(JSON.stringify(scope.seed[field]), /recovered/);
  });
  test(name + ' reject failed persistence without mutating displayed state', async () => {
    const scope = contextFor(start, end, name.startsWith('launcher') ? 'saveLauncherSettings' : 'saveConfigurationSettings');
    const before = JSON.stringify(scope.seed[field]);
    await assert.rejects(() => scope.tested({locale: 'fr-FR', sort: 'lastPlayed'}));
    assert.equal(JSON.stringify(scope.seed[field]), before);
  });
  test(name + ' update displayed state after acknowledged save', async () => {
    const scope = contextFor(start, end, name.startsWith('launcher') ? 'saveLauncherSettings' : 'saveConfigurationSettings', {
      fetch: async () => ({ok: true, json: async () => ({ok: true})}), osRequest: async () => ({ok: true}),
    });
    assert.equal(await scope.tested({locale: 'fr-FR', sort: 'lastPlayed'}), true);
    assert.ok(JSON.stringify(scope.seed[field]).includes('fr-FR'));
  });
}

test('native log tail keeps one request in flight and ignores a stopped tail response', async () => {
  const ticks = new Map(), requests = [], lines = [];
  let timer = 0;
  const scope = contextFor('  const logListeners = [];', '  async function launchProduct',
    '({startLogTail,stopLogTail,logListeners,cursor:()=>logCursor})', {
      setInterval: callback => {ticks.set(++timer, callback); return timer;},
      clearInterval: id => ticks.delete(id),
      fetch: url => new Promise(resolve => requests.push({url,resolve})),
    });
  scope.tested.logListeners.push(line => lines.push(line));
  scope.tested.startLogTail();
  const pending = ticks.get(timer)();
  ticks.get(timer)();
  assert.equal(requests.length,1,'a slow read does not create duplicate cursor requests');
  scope.tested.stopLogTail(); scope.tested.startLogTail();
  const current = ticks.get(timer)();
  requests[1].resolve({ok:true,json:async()=>({running:true,next:7,lines:['current']})});
  await current;
  requests[0].resolve({ok:true,json:async()=>({running:false,next:2,lines:['old']})});
  await pending;
  assert.deepEqual(lines,['current']);
  assert.equal(scope.tested.cursor(),7);
  assert.equal(ticks.size,1,'the old terminal reply cannot stop the current tail');
});

test('a newly acknowledged game starts a fresh log tail before old terminal replies', async () => {
  const ticks=new Map(), requests=[], lines=[]; let timer=0;
  const scope=contextFor('  const logListeners = [];','  async function launchQuickPlay',
    '({startLogTail,launchProduct,logListeners,cursor:()=>logCursor})',{
      trace(){},setInterval:callback=>{ticks.set(++timer,callback);return timer;},clearInterval:id=>ticks.delete(id),
      fetch:url=>String(url)==='/__launch'?Promise.resolve({ok:true,json:async()=>({pid:22})})
        :new Promise(resolve=>requests.push({url,resolve})),
    });
  scope.tested.logListeners.push(line=>lines.push(line));scope.tested.startLogTail();
  const old=ticks.get(timer)();
  await scope.tested.launchProduct('java','1.21.1',null,'fixture');
  const fresh=ticks.get(timer)();
  assert.equal(requests.length,2,'the new process does not wait on the previous process read');
  requests[0].resolve({ok:true,json:async()=>({running:false,next:90,lines:['old']})});await old;
  requests[1].resolve({ok:true,json:async()=>({running:true,next:1,lines:['new']})});await fresh;
  assert.deepEqual(lines,['new']);assert.equal(scope.tested.cursor(),1);assert.equal(ticks.size,1);
});

for (const action of ['cancel', 'pause', 'resume']) {
  test(action + ' download refuses when host refuses', async () => {
    const match = source.match(new RegExp(action + 'Download: async \\(\\) => \\{([\\s\\S]*?)\\n    \\},'));
    assert.ok(match);
    const sandbox = {osRequest: async () => ({ok: false, error: 'unsupported'}),
      installControl: async () => false, hostError:(_code,message)=>new Error(message)};
    vm.createContext(sandbox);
    const fn = vm.runInContext('(async () => {' + match[1] + '\n})', sandbox);
    await assert.rejects(fn(), /Unable to/);
  });
}

for (const [method, arg] of [['saveSkin', {id: 'new', name: 'New'}], ['activateSkin', {id: 'new'}], ['deleteSkin', 'old']]) {
  test(method + ' rejects storage failure without changing skin state', async () => {
    const scope = contextFor('  const skinHostCalls =', '  const common =', 'skinHostCalls');
    const before = JSON.stringify(scope.seed.skins);
    await assert.rejects(() => scope.tested[method](arg));
    assert.equal(JSON.stringify(scope.seed.skins), before);
  });
}

test('failed installation never publishes Succeeded', async () => {
  const scope = contextFor('  function trackDownload(', '  // --- launching the game', 'trackDownload', {
    LOADING_PHASES: {}, setTimeout() {},
    fetch: async () => ({ok: true, json: async () => ({active: false, phase: 'failed', error: 'download failed', bytesDone: 5, bytesTotal: 10})}),
    push() {}, markInstalled() { throw new Error('must not mark failed download installed'); },
  });
  await assert.rejects(() => scope.tested(), /download failed/);
  assert.notEqual(scope.seed.downloads.current.state, 'Succeeded');
});

test('cancelled installation never marks installed or permits automatic launch', async () => {
  const scope = contextFor('  function trackDownload(', '  // --- launching the game', 'trackDownload', {
    LOADING_PHASES: {}, setTimeout() {},
    fetch: async () => ({ok:true,json:async()=>({jobId:'owned',active:false,phase:'cancelled',controlStatus:'cancelled',bytesDone:5,bytesTotal:10})}),
    push() {}, markInstalled(){throw new Error('cancelled installation marked installed');},
  });
  await assert.rejects(scope.tested(), /cancel/i);
  assert.equal(scope.seed.downloads.current.state, 'Cancelled');
});

test('paused download keeps its owned job and exposes resume capabilities', async () => {
  const scope = contextFor('  function trackDownload(', '  // --- launching the game', 'trackDownload', {
    LOADING_PHASES:{},setTimeout(){},
    fetch:async()=>({ok:true,json:async()=>({jobId:'owned',active:true,paused:true,controlStatus:'paused',canPause:false,canResume:true,canCancel:true,phase:'assets',bytesDone:5,bytesTotal:10})}),
    push(){},markInstalled(){throw new Error('paused marked installed');},
  });
  scope.tested();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(scope.seed.downloads.current.state,'Paused');
  assert.equal(scope.seed.downloads.current.jobId,'owned');
  assert.equal(scope.seed.downloads.current.canResume,true);
  assert.equal(scope.seed.gameInstances.instances[0].downloadId,'owned');
  assert.equal(scope.seed.gameInstances.instances[0].state,'Downloading');
  assert.equal(scope.seed.downloads.pool.owned.state,'Paused');
});

test('download control carries ownership and rejects a stale response after another job starts', async () => {
  let release, sent;
  const scope = contextFor('  async function installControl(', '  function trackDownload(', 'installControl', {
    push(){},osRequest:(_url,body)=>{sent=body;return new Promise(resolve=>{release=resolve;});},
  });
  scope.seed.downloads={current:{jobId:'old',state:'Downloading'}};
  const pending=scope.tested('pause');
  assert.equal(sent.jobId,'old');
  scope.seed.downloads.current={jobId:'new',state:'Downloading'};
  release({ok:true,status:{jobId:'old',paused:true}});
  assert.equal(await pending,false);
  assert.equal(scope.seed.downloads.current.jobId,'new');
  assert.equal(scope.seed.downloads.current.state,'Downloading');
});

test('download tracking rejects a replaced job without marking it installed', async () => {
  const scope = contextFor('  function trackDownload(', '  // --- launching the game', 'trackDownload', {
    LOADING_PHASES:{},setTimeout(){},push(){},
    fetch:async()=>({ok:true,json:async()=>({jobId:'another',active:false,phase:'done',isInstalled:true})}),
    markInstalled(){throw new Error('replacement marked installed');},
  });
  await assert.rejects(scope.tested('owned'),/no longer current/i);
});

test('completed Microsoft sign-in selects that account instead of existing offline account', async () => {
  let current = {id: 'offline-old'};
  const online = {id: 'msa-online'};
  const accountRequests = [];
  const scope = contextFor('  const accountAction =', "  // The harness' own screens", 'microsoftSignIn', {
    trace() {}, setTimeout: callback => callback(),
    document: {createElement: () => ({click() {}, remove() {}}), body: {appendChild() {}}},
    fetch: async (url, options) => {
      if (url === '/__accounts') {
        const payload = JSON.parse(options.body);
        accountRequests.push(payload);
        const valid = payload.action === 'switch' && payload.id === online.id;
        if (valid) current = online;
        return {ok: valid, json: async () => valid
          ? {accounts: [online], activeAccountId: online.id}
          : {error: 'Unknown account action'}};
      }
      return {ok: true, json: async () => url.endsWith('signin')
        ? {url: 'https://example.invalid'} : {phase: 'done', account: online}};
    },
    refreshAccounts: async () => {}, publishAccounts() {}, activeRecord: () => current,
  });
  assert.equal((await scope.tested()).id, online.id);
  assert.deepEqual(accountRequests, [{action: 'switch', id: online.id}]);
});

test('concurrent skin saves preserve both acknowledged additions', async () => {
  let release;
  const gate = new Promise(resolve => {release = resolve;});
  let calls = 0, stored;
  const scope = contextFor('  const skinHostCalls =', '  const common =', 'skinHostCalls', {
    osRequest: async (_path, body) => {calls++; if (calls === 1) await gate; stored = body.customSkins; return {ok: true};},
  });
  const first = scope.tested.saveSkin({id: 'a'});
  const second = scope.tested.saveSkin({id: 'b'});
  await Promise.resolve();
  release();
  await Promise.all([first, second]);
  assert.ok(stored.a && stored.b);
  assert.ok(scope.seed.skins.customSkins.a && scope.seed.skins.customSkins.b);
});

function diskScope(fetch) {
  return contextFor('  const diskSpace =', '  const common =', 'diskSpace', {fetch});
}

test('disk queries return current host bytes for the install path and selected volume', async () => {
  const urls = [];
  const scope = diskScope(async url => {
    urls.push(url);
    return {ok: true, json: async () => ({value: urls.length === 1 ? 8192 : 4096})};
  });
  assert.equal((await scope.tested('path', 'D:\\My games & mods')).value, 8192);
  assert.equal((await scope.tested('volume', 'D:')).value, 4096);
  assert.equal(new URL(urls[0], 'http://fixture').searchParams.get('path'), 'D:\\My games & mods');
  assert.equal(new URL(urls[1], 'http://fixture').searchParams.get('volume'), 'D:');
});

test('default disk query leaves the target to the configured host game directory', async () => {
  const scope = diskScope(async url => {
    assert.equal(url, '/__disk_space');
    return {ok: true, json: async () => ({value: 0})};
  });
  assert.equal((await scope.tested('path')).value, 0);
});

for (const value of [undefined, '100000000000', -1, NaN, Infinity]) {
  test('invalid disk result is rejected: ' + String(value), async () => {
    const scope = diskScope(async () => ({ok: true, json: async () => ({value})}));
    await assert.rejects(() => scope.tested('path'), {code: 'UNABLE_TO_OPEN_FILE'});
  });
}

test('disk permission and connection errors never publish invented free space', async () => {
  const refused = diskScope(async () => ({ok: false, json: async () => ({error: 'Permission denied'})}));
  await assert.rejects(() => refused.tested('volume', 'D:'), {message: 'UNABLE_TO_OPEN_FILE', detail: 'Permission denied'});
  const disconnected = diskScope(async () => {throw new Error('Host disconnected');});
  await assert.rejects(() => disconnected.tested('path'), {code: 'UNABLE_TO_OPEN_FILE', message: 'UNABLE_TO_OPEN_FILE', detail: 'Could not read available disk space: Host disconnected'});
});

test('drive selection cannot claim to move game storage or change the displayed drive', async () => {
  const scope = contextFor('  const selectVolume =', '  const common =', 'selectVolume');
  scope.seed.volumes = {selectedVolumeName: 'C:', volumes: [{name: 'C:', isDefault: true}]};
  const before = JSON.stringify(scope.seed.volumes);
  assert.equal(await scope.tested('C:'), true);
  await assert.rejects(() => scope.tested('D:'), {code: 'FAILED_TO_SET_VOLUME'});
  assert.equal(JSON.stringify(scope.seed.volumes), before);
});

test('Quick Play launches the source-bound installation instead of the default', async () => {
  const calls = [];
  const scope = contextFor('  async function launchQuickPlay(', '  // Skin host calls.', 'launchQuickPlay', {
    launchProduct: async (...args) => {calls.push(args); return {ok: true};},
  });
  await scope.tested({id:'quick-id',source:'Java',javaInstance:{configId:'same-name-other-source'}}, '/home/general');
  assert.deepEqual(calls, [['java', undefined, '/home/general', 'same-name-other-source']]);
  for (const record of [null, {}, {source:'Bedrock'}, {source:'Java',javaInstance:{configId:'id',game:{type:'singleplayer'}}}]) {
    await assert.rejects(() => scope.tested(record), {code: 'FAILED_TO_LAUNCH_PRODUCT'});
  }
  assert.equal(calls.length, 1);
});

test('Quick Play save failure retains existing records and emits no success event', async () => {
  const pushed = [];
  const scope = contextFor('  const quickPlayRequest =', '  const common =', 'quickPlayRequest', {
    osRequest: async () => ({ok: false, error:'Disk full'}), pushSlice: name => pushed.push(name),
  });
  scope.seed.quickPlayData = {playRecords:[{id:'existing'}]};
  await assert.rejects(() => scope.tested('/__quickplay/delete', {all:true}), /Disk full/);
  assert.equal(scope.seed.quickPlayData.playRecords[0].id, 'existing');
  assert.deepEqual(pushed, []);
});

test('Quick Play mutations publish acknowledged state in order and recover after failure', async () => {
  let release;
  const gate = new Promise(resolve => {release = resolve;});
  const calls = [], pushes = [];
  const scope = contextFor('  const quickPlayRequest =', '  const common =', 'quickPlayRequest', {
    osRequest: async (_path, body) => {
      calls.push(body.id);
      if (body.id === 'first') await gate;
      if (body.id === 'failed') return {ok:false,error:'Write failed'};
      return {ok:true,quickPlayData:{playRecords:[{id:body.id}]}};
    }, pushSlice: name => pushes.push(name),
  });
  const first = scope.tested('/__quickplay/update',{id:'first'});
  const second = scope.tested('/__quickplay/update',{id:'second'});
  await Promise.resolve();await Promise.resolve();
  assert.deepEqual(calls, ['first']);
  release();await Promise.all([first, second]);
  assert.equal(scope.seed.quickPlayData.playRecords[0].id, 'second');
  assert.deepEqual(pushes, ['quickPlayData','quickPlayData']);
  await assert.rejects(() => scope.tested('/__quickplay/update',{id:'failed'}), /Write failed/);
  await scope.tested('/__quickplay/delete',{id:'third'});
  assert.equal(scope.seed.quickPlayData.playRecords[0].id, 'third');
});

test('file picker failure is reported through both native callback and path contracts', async () => {
  for (const out of [null, {ok:false,error:'Picker failed'}]) {
    const scope = contextFor('  const chooseFile =', '  const common =', 'chooseFile', {osRequest: async () => out});
    await assert.rejects(() => scope.tested({mode:'open_file'}), {code:'UNABLE_TO_OPEN_FILE'});
    const errors = [];
    assert.equal(await scope.tested({onSuccess(){throw new Error('must not select');},onError: error=>errors.push(error)}), false);
    assert.equal(errors.length, 1);
  }
});

test('picker cancellation stays distinct from failure and selected data reaches the callback', async () => {
  const requests = [];
  const scope = contextFor('  const chooseFile =', '  const common =', 'chooseFile', {
    osRequest: async (path, body) => {
      requests.push({path,body});
      return path === '/__dialog' ? {path:'C:\\fixture\\cape.png'} : {name:'cape.png',base64:'bytes'};
    },
  });
  let selected;
  assert.equal(await scope.tested({onSuccess: (...args)=>{selected=args;}}), true);
  assert.deepEqual(selected, ['cape.png','bytes','C:\\fixture\\cape.png']);
  assert.equal(requests[1].body.path, 'C:\\fixture\\cape.png');
  const cancelled = contextFor('  const chooseFile =', '  const common =', 'chooseFile', {osRequest: async () => ({path:''})});
  assert.equal(await cancelled.tested({mode:'open_file'}), '');
  assert.equal(await cancelled.tested({onSuccess(){throw new Error('must not select');},onError(){throw new Error('not an error');}}), false);
});

test('unreadable selected file does not invoke success or disappear silently', async () => {
  const scope = contextFor('  const chooseFile =', '  const common =', 'chooseFile', {
    osRequest: async path => path === '/__dialog' ? {path:'C:\\fixture\\cape.png'} : {error:'Read permission denied'},
  });
  let error;
  assert.equal(await scope.tested({onSuccess(){throw new Error('not readable');},onError: message=>{error=message;}}), false);
  assert.equal(error,'Read permission denied');
});

function profileScope(active, accounts = [], action = async () => true) {
  const extras = {activeRecord: () => active, accountState: {accounts}, accountAction: action};
  if (source.includes('  const profileHostCalls =')) {
    return contextFor('  const profileHostCalls =', '  const handlers =', 'profileHostCalls', extras);
  }
  const sandbox = {hostError: (code, message) => Object.assign(new Error(message), {code}), ...extras};
  vm.createContext(sandbox);
  const body = source.slice(source.indexOf('      createMinecraftProfile:'), source.indexOf('      cleanUnusedSecureTokens:'));
  sandbox.tested = vm.runInContext('({' + body + '})', sandbox);
  return sandbox;
}

test('profile operations reject online or absent accounts without fabricated success', async () => {
  for (const account of [null, {id:'online',offline:false}]) {
    const scope = profileScope(account, [], () => {throw new Error('must not mutate');});
    for (const method of ['createMinecraftProfile','changeMinecraftProfileName','profileNameAvailable']) {
      await assert.rejects(() => scope.tested[method]('Player'), /unavailable|offline account/i);
    }
  }
});

test('offline profile availability returns native statuses and mutation requires valid available name', async () => {
  const active = {id:'one',offline:true,javaProfileName:'Before'};
  const calls = [];
  const scope = profileScope(active, [active,{id:'two',javaProfileName:'Taken'}], async (...args) => {calls.push(args);});
  assert.equal((await scope.tested.profileNameAvailable('Taken')).status,'DUPLICATE');
  assert.equal((await scope.tested.profileNameAvailable('!')).status,'NOT_ALLOWED');
  assert.equal((await scope.tested.profileNameAvailable('Next_Name')).status,'AVAILABLE');
  await assert.rejects(() => scope.tested.createMinecraftProfile('Taken'));
  await assert.rejects(() => scope.tested.changeMinecraftProfileName('!'));
  assert.equal(await scope.tested.createMinecraftProfile('Next_Name'),true);
  assert.deepEqual(JSON.parse(JSON.stringify(calls)),[['rename',{id:'one',name:'Next_Name'}]]);
});

test('native configuration settings root writes and rejects failed persistence', async () => {
  const expression = source.match(/updateConfigurationData:\s*([^\n]+),/)[1];
  const calls=[];
  const scope=contextFor('  const saveConfigurationSettings =','  // Native helpers','saveConfigurationSettings',{
    osRequest:async(path,body)=>{calls.push({path,body});return {ok:false,error:'Disk full'};}
  });
  const invoke=vm.runInContext('('+expression+')',scope);
  await assert.rejects(()=>invoke({enableGameOutput:true}),/Disk full/);
  assert.equal(calls.length,1);
  assert.equal(calls[0].path,'/__configuration_settings');
  assert.equal(calls[0].body.enableGameOutput,true);
  assert.equal(scope.seed.configurationData.settings.sort,'name');
});

test('configuration setting acknowledgments retain unrelated flags and accept authoritative settings', async () => {
  const scope=contextFor('  const saveConfigurationSettings =','  // Native helpers','saveConfigurationSettings',{
    osRequest:async()=>({ok:true})
  });
  await scope.tested({enableGameOutput:true});
  assert.equal(scope.seed.configurationData.settings.sort,'name');
  assert.equal(scope.seed.configurationData.settings.enableGameOutput,true);
  scope.osRequest=async()=>({ok:true,settings:{enableSnapshots:false,profileSorting:'ByName',enableGameOutput:true}});
  await scope.tested({enableSnapshots:false});
  assert.equal(scope.seed.configurationData.settings.profileSorting,'ByName');
  assert.equal(scope.seed.configurationData.settings.enableGameOutput,true);
});

test('callable Quick Play alias preserves nested getters and delete-all used by native facade', async () => {
  const calls=[];
  const scope=contextFor('  const FACADE_ALIASES =','  // Small pure helpers','handlers',{
    handlers:{products:{quickPlay:async(record)=>{calls.push('launch:'+record.id);return true;}},
      quickPlay:{getQuickPlayData:()=>({playRecords:[{id:'recent'}]}),deleteAllQuickPlayInstances:async()=>{calls.push('delete');return true;}}}
  });
  assert.equal(await scope.tested.quickPlay({id:'recent'}),true);
  assert.equal(scope.tested.quickPlay.getQuickPlayData().playRecords[0].id,'recent');
  assert.equal(await scope.tested.deleteAllQuickPlayInstances(),true);
  assert.deepEqual(calls,['launch:recent','delete']);
});
