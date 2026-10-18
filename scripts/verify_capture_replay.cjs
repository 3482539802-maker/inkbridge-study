// Execute the production controller with every UI timer paused. Socket/native
// callbacks still run, as in the failure observed during background capture.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const ts = require(process.argv[2] || 'D:/DevEco Studio/sdk/default/openharmony/ets/build-tools/ets-loader/node_modules/typescript');
const source = fs.readFileSync(path.join(__dirname, '../app/entry/src/main/ets/ink/CaptureController.ets'), 'utf8');
const code = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2021}}).outputText;
const backgroundCode = ts.transpileModule(fs.readFileSync(path.join(__dirname,
  '../app/entry/src/main/ets/ink/CaptureBackground.ets'), 'utf8'),
  {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2021}}).outputText;
const declaredModes = JSON.parse(fs.readFileSync(path.join(__dirname,
  '../app/entry/src/main/module.json5'), 'utf8')).module.abilities.find(a => a.name === 'EntryAbility').backgroundModes;

function harness(options = {}) {
  let now = 100000, input, fd = 0, failNetwork = false, timerRegistrations = 0;
  let resolveGrant, screenCallback, cancelListener;
  let failedSyncedReceipt = false, failedRawWrite = false;
  const operations = [];
  const bg = {
    ContinuousTaskRequest: class {
      isModeSupported() { throw Error('Legacy parent-mode query must not gate a screen-recording request'); }
    },
    BackgroundTaskMode: { MODE_AV_PLAYBACK_AND_RECORD: 12 },
    BackgroundTaskSubmode: { SUBMODE_SCREEN_RECORD_NORMAL_NOTIFICATION: 7 },
    on(type, callback) { assert.equal(type, 'continuousTaskCancel'); cancelListener = callback; },
    off(type, callback) { assert.equal(callback, cancelListener); cancelListener = undefined; },
    startBackgroundRunning(context, request) {
      assert.deepEqual(Array.from(request.backgroundTaskModes), [12]);
      assert.deepEqual(Array.from(request.backgroundTaskSubmodes), [7]);
      operations.push('background_request');
      // Real screen-record submode 7 maps to legacy AUDIO_RECORDING on this
      // service boundary. Firmware rejected the parent-only declaration.
      if (!declaredModes.includes('audioRecording')) {
        return Promise.reject({code: 9800005, message: 'The bgMode is invalid.'});
      }
      if (options.failGrant) return Promise.reject({code: 9800005, message: 'The bgMode is invalid.'});
      if (options.delayGrant) return new Promise(resolve => {resolveGrant = resolve;});
      return Promise.resolve({continuousTaskId: 73});
    },
    stopBackgroundRunning(context, id) { assert.equal(id, 73); operations.push('background_release'); return Promise.resolve(); }
  };
  const files = new Map(), opened = new Map(), sessions = new Map();
  const fileIo = {OpenMode: {CREATE: 1, READ_WRITE: 2, TRUNC: 4, READ_ONLY: 8},
    listFileSync() { return []; },
    openSync(name, mode) {
      if (!files.has(name) || mode & 4) files.set(name, Buffer.alloc(0));
      opened.set(++fd, name); return {fd};
    },
    writeSync(id, data) {
      const name = opened.get(id), bytes = Buffer.from(data);
      if (options.shortRawWrite && name.endsWith('.jsonl')) {
        if (failedRawWrite) return 0;
        failedRawWrite = true;
        const partial = bytes.subarray(0, 17);
        files.set(name, Buffer.concat([files.get(name), partial])); return partial.length;
      }
      files.set(name, Buffer.concat([files.get(name), bytes])); return bytes.length;
    },
    readSync(id, buffer, options) {
      const bytes = files.get(opened.get(id)).subarray(options.offset, options.offset + options.length);
      new Uint8Array(buffer).set(bytes); return bytes.length;
    },
    fsyncSync() {}, closeSync(id) { opened.delete(id); },
    truncateSync(id, length) { const name = opened.get(id); files.set(name, files.get(name).subarray(0, length)); },
    renameSync(a, b) {
      if (options.failFirstSyncedReceipt && !failedSyncedReceipt && JSON.parse(files.get(a)).synced) {
        failedSyncedReceipt = true; throw Error('receipt rename failed');
      }
      files.set(b, files.get(a)); files.delete(a);
    }
  };
  const socket = {constructTCPSocketInstance() {
    const listeners = {};
    return {on(kind, callback) { listeners[kind] = callback; },
      connect() { return failNetwork ? Promise.reject(Error('offline')) : Promise.resolve(); },
      close() { return Promise.resolve(); },
      send({data}) {
        const frame = JSON.parse(data), id = frame.session_id;
        let state = sessions.get(id);
        if (frame.type === 'session') {
          if (!state) { state = {raw: '', complete: false}; sessions.set(id, state); }
        } else if (frame.type === 'batch') {
          assert.equal(frame.offset, state.raw.length);
          state.raw += frame.data;
        } else if (frame.type === 'session_end') {
          assert.equal(frame.byte_length, state.raw.length);
          assert.equal(frame.point_count, state.raw ? state.raw.trim().split('\n').length : 0);
          state.complete = true;
        }
        const ack = {type: 'ack', session_id: id, offset: state.raw.length, complete: frame.type === 'session_end'};
        queueMicrotask(() => listeners.message({message: Buffer.from(JSON.stringify(ack) + '\n')}));
        return Promise.resolve();
      }
    };
  }};
  const native = {beginScreen(callback) {
      operations.push('screen_requested');
      screenCallback = callback;
      queueMicrotask(() => {
        if (options.backgroundDuringConsent) controller.setForeground(false);
        operations.push('screen_authorized'); callback(0);
      }); return 0;
    },
    start(callback) { operations.push('input_start'); input = callback; return 0; }, status() { return {dropped_count: 0}; },
    stop() { return Promise.resolve(options.nativeStopCode || 0); }, endScreen() {
      operations.push('screen_end'); if (options.failScreenRelease) throw Error('screen release failed');
    }};
  const exportsObject = {};
  const platform = {Date: {now() {return now;}},
    setInterval() { timerRegistrations++; return timerRegistrations; },
    setTimeout() { return 1; }, clearInterval() {}, clearTimeout() {},
    require(name) {
      if (name === '@kit.AbilityKit') return {
        abilityAccessCtrl: {createAtManager() { return {checkAccessTokenSync() {return 0;}}; }},
        wantAgent: {getWantAgent() {operations.push('agent_create'); return Promise.resolve({});},
          cancel() {operations.push('agent_cancel'); return Promise.resolve();},
          OperationType: {START_ABILITY: 1}, WantAgentFlags: {UPDATE_PRESENT_FLAG: 1}}
      };
      if (name === '@kit.BackgroundTasksKit') return {backgroundTaskManager: bg};
      if (name === '@kit.CoreFileKit') return {fileIo};
      if (name === '@kit.NetworkKit') return {socket};
      if (name === '@kit.ArkTS') return {util: {
        generateRandomUUID() {return 'paused-timer-test';},
        TextEncoder: class {encodeInto(text) {return new TextEncoder().encode(text);}},
        TextDecoder: {create() {return {decodeToString(bytes) {return new TextDecoder().decode(bytes);}};}}
      }};
      if (name === '@kit.PerformanceAnalysisKit') return {hilog: {info() {}, error() {}}};
      if (name === 'libink_native.so') return {default: native};
      if (name === './CaptureClock') return {CaptureClock: class {start() {} observe() {} tick() {} stop() {
        if (options.failClockStop) throw Error('clock stop failed');
      }}};
      if (name === './CaptureBackground') return backgroundExports;
      throw Error(name);
    }};
  const backgroundExports = {};
  vm.runInNewContext(backgroundCode, {...platform, exports: backgroundExports});
  vm.runInNewContext(code, {...platform, exports: exportsObject});
  const controller = exportsObject.captureController;
  const context = {filesDir: '/mock', applicationInfo: {accessTokenId: 1}};
  return {controller, sessions, files, operations,
    grant() {resolveGrant({continuousTaskId: 73});},
    cancel() {cancelListener({id: 73, reason: 2});},
    repeatAuthorized() {screenCallback(0);},
    async start() {
      controller.setForeground(!options.startInBackground);
      await controller.start(context); await new Promise(setImmediate);
    },
    offline(value) {failNetwork = value;},
    advance(ms) {now += ms;},
    emit(seq) {input({type: 'point', seq, action: 2, pointer_id: 1, x: 20, y: 30,
      action_time: seq*1000, down_time: 1000, pressure: .5, tool_type: 1, window_id: 7, display_id: 0});}
  };
}

(async () => {
  const hidden = harness({startInBackground: true});
  await hidden.start();
  assert.equal(hidden.operations.includes('screen_requested'), false, 'background click cannot start consent or request a task');
  assert.match(hidden.controller.message, /前台/);

  const consent = harness({backgroundDuringConsent: true});
  await consent.start();
  assert.equal(consent.operations.includes('background_request'), false, 'wait until the authorization UI returns to the app');
  consent.controller.setForeground(true);
  await new Promise(setImmediate);
  assert.equal(consent.controller.running, true);
  consent.controller.setForeground(false);
  assert.equal(consent.controller.running, true, 'returning to Notes must preserve capture');
  await consent.controller.stopCapture();

  const abandoned = harness({backgroundDuringConsent: true});
  await abandoned.start();
  await abandoned.controller.stopCapture('authorization_cancelled');
  abandoned.controller.setForeground(true);
  await new Promise(setImmediate);
  assert.equal(abandoned.operations.includes('background_request'), false, 'returning after cancellation cannot restart');

  const live = harness();
  await live.start();
  assert.equal(live.sessions.size, 1, 'start handshake must not wait for a UI timer');
  live.advance(1000);
  for (let i=1; i<=32; i++) live.emit(i);
  await new Promise(setImmediate);
  assert.equal(live.sessions.get('paused-timer-test').raw.trim().split('\n').length, 32,
    'native flush must initiate sync with all UI timers paused');
  live.emit(33);
  await live.controller.stopCapture();
  await new Promise(setImmediate);
  assert.equal(live.sessions.get('paused-timer-test').complete, true, 'stop must flush and finish without a timer');
  assert.equal(live.controller.sessions[0].synced, true);

  const receipt = harness({failFirstSyncedReceipt: true});
  await receipt.start(); receipt.emit(1);
  await receipt.controller.stopCapture(); await new Promise(setImmediate);
  assert.equal(receipt.sessions.get('paused-timer-test').complete, true);
  assert.equal(receipt.controller.sessions[0].synced, false, 'failed local receipt must remain retryable');
  receipt.advance(40000); receipt.controller.retry(); await new Promise(setImmediate);
  assert.equal(receipt.controller.sessions[0].synced, true);
  assert.equal(JSON.parse(receipt.files.get('/mock/ink-paused-timer-test.session.json')).synced, true);

  const stopFailed = harness({nativeStopCode: 7});
  await stopFailed.start(); stopFailed.emit(1);
  await stopFailed.controller.stopCapture(); await new Promise(setImmediate);
  assert.equal(stopFailed.controller.sessions[0].loss_accounting_complete, false);
  assert.equal(stopFailed.controller.sessions[0].state, 'interrupted');

  const partial = harness({shortRawWrite: true});
  await partial.start();
  for (let i=1; i<=32; i++) partial.emit(i);
  await new Promise(setImmediate);
  assert.equal(partial.files.get('/mock/ink-paused-timer-test.jsonl').length, 0, 'partial uncommitted raw tail rolls back');
  assert.equal(partial.controller.sessions[0].point_count, 0);
  assert.equal(partial.controller.sessions[0].loss_accounting_complete, false);
  assert.equal(partial.controller.sessions[0].state, 'interrupted');

  for (const options of [{failClockStop: true}, {failScreenRelease: true}]) {
    const cleanup = harness(options); await cleanup.start(); cleanup.emit(1);
    await cleanup.controller.stopCapture(); await new Promise(setImmediate);
    assert.equal(cleanup.controller.running, false); assert.equal(cleanup.controller.stopping, false);
    assert.equal(cleanup.controller.sessions[0].synced, true, 'optional/release failure cannot strand raw ACK');
    assert.equal(cleanup.controller.sessions[0].point_count, 1);
    assert.ok(cleanup.operations.includes('background_release'));
    assert.equal(cleanup.controller.sessions[0].state, options.failScreenRelease ? 'interrupted' : 'completed');
  }

  const backlog = harness();
  backlog.offline(true);
  await backlog.start();
  // > eight batches: successful replay must continue through socket events.
  for (let i=1; i<=7001; i++) backlog.emit(i);
  await new Promise(setImmediate);
  backlog.offline(false);
  backlog.advance(40000);
  await backlog.controller.stopCapture();
  await new Promise(setImmediate);
  const recovered = backlog.sessions.get('paused-timer-test');
  assert.equal(recovered.complete, true, 'backlog must drain beyond eight batches');
  assert.equal(recovered.raw.trim().split('\n').length, 7001);
  assert.equal(backlog.controller.sessions[0].synced, true);
  assert.equal(Buffer.from(recovered.raw).equals(backlog.files.get('/mock/ink-paused-timer-test.jsonl')), true);
  assert.ok(live.operations.indexOf('screen_authorized') < live.operations.indexOf('background_request'));
  assert.ok(live.operations.indexOf('background_request') < live.operations.indexOf('input_start'));
  assert.ok(live.operations.indexOf('screen_end') < live.operations.indexOf('background_release'));

  for (const options of [{failGrant: true}]) {
    const denied = harness(options);
    await denied.start();
    assert.equal(denied.controller.running, false, 'no unprotected capture on a refused background grant');
    assert.equal(denied.sessions.size, 0);
    assert.ok(denied.operations.includes('screen_end'));
    assert.ok(denied.operations.includes('agent_cancel'));
    assert.ok(denied.controller.message.includes('9800005'), 'preserve the actual service refusal for the user');
    const diagnostic = JSON.parse(denied.files.get('/mock/capture-background.json').toString());
    assert.ok(diagnostic.some(e => e.kind === 'request_failed' && e.code === 9800005 &&
      e.detail === 'start_background: The bgMode is invalid.'));
  }
  const racing = harness({delayGrant: true});
  await racing.start();
  racing.repeatAuthorized();
  assert.equal(racing.operations.filter(x => x === 'background_request').length, 1);
  const stopped = racing.controller.stopCapture();
  await new Promise(setImmediate);
  assert.equal(racing.controller.stopping, true);
  racing.grant();
  await stopped;
  assert.ok(!racing.operations.includes('input_start'), 'late grant cannot restart a user-stopped capture');
  assert.equal(racing.operations.filter(x => x === 'background_release').length, 1);
  assert.equal(racing.sessions.size, 0);

  const cancelled = harness();
  await cancelled.start();
  cancelled.emit(1);
  cancelled.cancel();
  await new Promise(setImmediate);
  assert.equal(cancelled.controller.running, false);
  assert.equal(cancelled.controller.sessions[0].stop_reason, 'background_cancel_2');
  assert.equal(cancelled.controller.sessions[0].state, 'interrupted');
  assert.equal(cancelled.controller.sessions[0].point_count, 1);
  assert.ok(!cancelled.operations.includes('background_release'), 'system-removed task is not cancelled twice');
  assert.equal(cancelled.operations.filter(x => x === 'background_request').length, 1, 'no automatic renewal');
  console.log('Production CaptureController: paused-timer start, native callback sync, stop and >8-batch recovery passed; raw bytes preserved.');
  console.log('Production CaptureBackground: screen authorization ordering, matched mode/submode, grant refusal, stop/grant race, duplicate STARTED and system cancellation passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
