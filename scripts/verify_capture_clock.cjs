// Run the production sidecar writer with simulated platform clock/files only.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const ts = require(process.argv[2] || 'D:/DevEco Studio/sdk/default/openharmony/ets/build-tools/ets-loader/node_modules/typescript');
const source = fs.readFileSync(path.join(__dirname, '../app/entry/src/main/ets/ink/CaptureClock.ets'), 'utf8');
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2021 } }).outputText;
let now = 1000, saved, writes = 0, fail = false, short = false, serial = 0;
let nativeEnabled = false, nativeStarted = 0, nativeStopped = 0;
const files = new Map(), handles = new Map();
const mockFs = { OpenMode: { CREATE: 1, READ_WRITE: 2, TRUNC: 4 },
  openSync(name) { if (fail) throw Error('full'); const fd = ++serial; handles.set(fd, name); return {fd}; },
  writeSync(fd, text) { files.set(handles.get(fd), text); writes++; return short ? text.length-1 : text.length; },
  fsyncSync() { if (fail) throw Error('fsync'); },
  closeSync(fd) { handles.delete(fd); },
  renameSync(a,b) { files.set(b,files.get(a)); files.delete(a); if (b.endsWith('.clock.json')) saved=JSON.parse(files.get(b)); } };
const exportsObject = {};
vm.runInNewContext(code, { exports: exportsObject, Date: {now() {return now;}}, Error,
  require(name) {
    if (name === '@kit.CoreFileKit') return {fileIo: mockFs};
    assert.equal(name, 'libink_native.so');
    return { default: {
      startClock() {nativeStarted++; return nativeEnabled;}, stopClock() {nativeStopped++;},
      clockSample() { return {monotonic_before_us: now*1000,
      monotonic_after_us: now*1000+1, boottime_us: now*1000+90000000, unix_ms: now}; }} };
  } });
const clock = new exportsObject.CaptureClock();
clock.start('/mock', 'session');
for (let i=1; i<=1440; i++) { now += 5001; clock.observe(now*1000-1000, i); }
assert.equal(saved.observations.length, 32);
assert.equal(saved.anchors.length, 32);
assert.equal(saved.observations[0].seq, 1409);
assert.equal(saved.observations[31].seq, 1440);
assert.ok(saved.history_segments >= 90, '2-hour immutable checkpoints');
const recovered = new Map();
for (let i=0; i<saved.history_segments; i++) {
  const segment = JSON.parse(files.get(`/mock/ink-session.clock-${i}.json`));
  assert.ok(segment.anchors.length <= 32);
  assert.ok(segment.observations.length <= 32);
  for (const o of segment.observations) recovered.set(o.seq, o);
}
for (const o of saved.observations) recovered.set(o.seq, o);
assert.equal(recovered.size, 1440, 'early calibration survives ring rotation without PC');
const previous = writes;
clock.observe(now*1000, 1441);
assert.equal(writes, previous, '5-second observation rate limit');
fail = true;
now += 6000;
assert.doesNotThrow(() => clock.observe(now*1000, 1442), 'optional metadata failure must not break capture');
fail = false;
short = true;
now += 6000;
const published = files.get('/mock/ink-session.clock.json');
clock.observe(now*1000, 1443);
assert.equal(files.get('/mock/ink-session.clock.json'),published,'short writes cannot publish partial latest');
short = false;
now += 1;
clock.stop();
assert.equal(saved.stopped,true,'fallback must publish a final tail marker');
assert.equal(handles.size,0,'all descriptors close on failure');
now += 1;
clock.start('/mock', 'other');
assert.equal(saved.observations.length, 0, 'new session clears previous evidence');
assert.equal(saved.event_clock_verified, false);
assert.equal(saved.stopped,false,'a new recording must not inherit the final marker');
fail = true;
clock.stop();
assert.equal(saved.stopped,false,'a failed final write must not fake a stopped sidecar');
fail = false;
nativeEnabled = true;
const beforeNative = writes;
clock.start('/mock','native');
now += 10000;
clock.observe(now*1000,1);
clock.tick(true,true);
assert.equal(writes,beforeNative,'ArkTS must not overwrite native-produced history');
clock.stop();
assert.equal(nativeStopped,1,'native history closes once');
assert.equal(writes,beforeNative);
assert.equal(nativeStarted,3);
console.log('Production CaptureClock: 2-hour checkpoints preserve all 1440 observations; bounded RAM, throttling, short/full disk failure and session reset passed.');
