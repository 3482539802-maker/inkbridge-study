// Run the production controller with mocked platform boundaries. No device/UI actions.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const ts = require(process.argv[2] || 'D:/DevEco Studio/sdk/default/openharmony/ets/build-tools/ets-loader/node_modules/typescript');
const source = fs.readFileSync(path.join(__dirname, '../app/entry/src/main/ets/ink/NotificationCapture.ets'), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2021 } }).outputText;

function fixture(failure) {
  const calls = { starts: 0, stops: 0, publishes: [], cancels: 0, unsubscribes: 0, delays: 0, releases: 0, launches: 0 };
  let handler, wanted, expireDelay;
  const capture = { running: false, authorizing: false, stopping: false, message: 'idle',
    async start() { calls.starts++; this.authorizing = true; this.message = 'awaiting authorization'; },
    async stopCapture() { calls.stops++; this.running = false; this.authorizing = false; this.message = 'stopped'; } };
  const notification = { async isNotificationEnabled() { return true; },
    async publish(request) { if (failure === 'publish') throw { code: 42 }; calls.publishes.push(request); },
    async cancel() { calls.cancels++; }, SlotType: { SERVICE_INFORMATION: 1 },
    ContentType: { NOTIFICATION_CONTENT_BASIC_TEXT: 0 } };
  const modules = {
    '@kit.AbilityKit': { wantAgent: { OperationType: { SEND_COMMON_EVENT: 4, START_ABILITY: 1 }, WantAgentFlags: { UPDATE_PRESENT_FLAG: 1 },
      async getWantAgent(request) {
        assert.equal(request.wants[0].bundleName, 'com.example.inkbridge');
        if (request.actionType === 1) {
          assert.equal(request.wants[0].abilityName, 'EntryAbility');
          assert.equal(request.wants[0].parameters, undefined, 'navigation must not auto-start capture');
        } else assert.equal(request.actionType, 4);
        return { event: request.wants[0].action, operation: request.actionType };
      },
      trigger(agent, _, callback) {
        assert.equal(agent.operation, 4, 'production must never trigger the navigation WantAgent');
        handler({ event: agent.event }); callback({ code: 0 });
      },
      async cancel() {} } },
    '@kit.NotificationKit': { notificationManager: notification },
    '@kit.BasicServicesKit': { commonEventManager: {
      async createSubscriber(info) { wanted = info.events; return {}; },
      async subscribeToEvent(_, callback) { if (failure === 'subscription') throw { code: 43 }; handler = callback; },
      unsubscribe() { calls.unsubscribes++; } } },
    '@kit.ArkTS': { util: { generateRandomUUID() { return 'test-nonce'; } } },
    '@kit.PerformanceAnalysisKit': { hilog: { info() {} } },
    '@kit.BackgroundTasksKit': { backgroundTaskManager: {
      requestSuspendDelay(_, callback) {
        calls.delays++;
        if (failure === 'delay') throw { code: 9900002 };
        expireDelay = callback;
        return { requestId: 1, actualDelayTime: 180000 };
      }, cancelSuspendDelay() { calls.releases++; } } },
    '@kit.CoreFileKit': { fileIo: { OpenMode: { CREATE: 1, READ_WRITE: 2, TRUNC: 4 },
      openSync() { throw Error('Simulated optional diagnostics unavailable'); } } },
    './CaptureController': { captureController: capture }
  };
  const exports = {};
  vm.runInNewContext(compiled, { exports, require(name) {
    assert.ok(Object.hasOwn(modules, name), `Unexpected platform dependency ${name}`); return modules[name];
  }, setInterval() { return 1; }, clearInterval() {}, setTimeout() { return 2; }, clearTimeout() {}, Date, Error, Math });
  return { controller: exports.notificationCapture, calls, capture,
    expire() { expireDelay(); }, fire(index) { handler({ event: wanted[index] }); },
    oldStart() { handler({event: 'com.example.inkbridge.capture.START.test-nonce'}); } };
}

(async () => {
  const f = fixture();
  await f.controller.enable({ filesDir: '/mock' });
  assert.equal(f.controller.enabled, true);
  assert.equal(f.calls.publishes.length, 1);
  assert.equal(f.calls.starts, 0, 'enabling must not start capture');
  assert.equal(f.calls.delays, 1, 'one finite background grant');
  assert.equal(f.calls.publishes[0].actionButtons[0].wantAgent.operation, 1);
  assert.match(f.calls.publishes[0].actionButtons[0].title, /打开采集页/);
  f.oldStart();
  assert.equal(f.calls.starts, 0, 'old common-event START cannot run capture in background');
  f.capture.authorizing = true; // User pressed the foreground page button.
  await f.controller.refresh();
  assert.match(f.calls.publishes.at(-1).actionButtons[0].title, /取消/);
  const count = f.calls.publishes.length;
  await f.controller.refresh();
  assert.equal(f.calls.publishes.length, count, 'no duplicate idle polling notifications');
  assert.equal(f.calls.publishes.at(-1).actionButtons[0].wantAgent.operation, 4);
  f.fire(0);
  await Promise.resolve();
  assert.equal(f.calls.stops, 1);
  f.controller.disable();
  f.fire(0);
  assert.equal(f.calls.starts, 0, 'notification never starts capture directly');
  const expired = fixture();
  await expired.controller.enable({ filesDir: '/mock' });
  expired.expire();
  expired.fire(0);
  assert.equal(expired.calls.starts, 0, 'expired start events must never launch consent later');
  assert.equal(expired.controller.enabled, false, 'remove inactive notification on expiration');
  const active = fixture();
  await active.controller.enable({ filesDir: '/mock' });
  active.capture.authorizing = false;
  active.capture.running = true;
  await active.controller.refresh();
  assert.equal(active.calls.releases, 1, 'release temporary grant once screen capture starts');
  active.expire();
  assert.equal(active.controller.enabled, true, 'retain stop while capturing');
  active.fire(0);
  await active.controller.refresh();
  assert.equal(active.controller.enabled, false, 'after a completed recording do not offer a frozen idle restart');
  assert.equal(active.calls.delays, 1, 'never renew background grant automatically');
  const waiting = fixture();
  await waiting.controller.enable({ filesDir: '/mock' });
  waiting.capture.authorizing = true;
  waiting.controller.expire();
  assert.equal(waiting.calls.releases, 0, 'do not release authorization budget at arm timeout');
  waiting.capture.authorizing = false;
  waiting.capture.message = 'authorization_timeout';
  await waiting.controller.refresh();
  assert.equal(waiting.calls.releases, 1);
  assert.equal(waiting.controller.enabled, false);
  for (const failure of ['subscription', 'publish', 'delay']) {
    const failed = fixture(failure);
    await failed.controller.enable({ filesDir: '/mock' });
    assert.equal(failed.controller.enabled, false);
    assert.match(failed.controller.message, /失败|未开启/);
    assert.equal(failed.calls.starts, 0);
  }
  console.log('PASS: production event self-check, action routing, authorization guard, stop/cancel, no auto-start, no repeated notifications, bounded suspend grant, expired events rejected, no automatic renewal, release on capture/stop and platform failures. Device verification still required.');
})().catch(error => { console.error(error); process.exitCode = 1; });
