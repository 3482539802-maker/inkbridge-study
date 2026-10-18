import { fileIo as fs } from '@kit.CoreFileKit';
import inkNative, { ClockSample } from 'libink_native.so';

interface EventClockObservation {
  action_time_us: number;
  seq: number;
  callback_clock: ClockSample;
}

interface CaptureClockState {
  history_format: number;
  history_segments: number;
  session_id: string;
  anchors: Array<ClockSample>;
  last_action_time_us: number;
  last_callback_clock: ClockSample | undefined;
  event_clock_verified: boolean;
  observations: Array<EventClockObservation>;
  stopped: boolean;
}

export class CaptureClock {
  private nativeRecorder: boolean = false;
  private path: string = '';
  private state: CaptureClockState | undefined = undefined;
  private nextSample: number = 0;
  private nextObservation: number = 0;
  private anchorsSinceCheckpoint: number = 0;

  start(directory: string, sessionId: string): void {
    try {
      this.nativeRecorder = inkNative.startClock(directory, sessionId);
      if (this.nativeRecorder) return;
    } catch (error) { /* Legacy fallback stays independently identified. */ }
    this.path = `${directory}/ink-${sessionId}.clock.json`;
    this.state = { history_format: 1, history_segments: 0, session_id: sessionId, anchors: [], last_action_time_us: 0,
      last_callback_clock: undefined, event_clock_verified: false, observations: [], stopped: false };
    this.anchorsSinceCheckpoint = 0;
    this.tick(true);
    this.nextObservation = 0;
  }
  observe(actionTime: number, seq: number): void {
    if (this.nativeRecorder) return;
    if (!this.state || Date.now() < this.nextObservation) return;
    this.nextObservation = Date.now() + 5000;
    try {
      this.state.last_action_time_us = actionTime;
      this.state.last_callback_clock = inkNative.clockSample();
      this.state.observations.push({ action_time_us: actionTime, seq: seq,
        callback_clock: this.state.last_callback_clock });
      if (this.state.observations.length > 32) this.state.observations.shift();
      this.tick(true);
    } catch (error) { /* Optional metadata only. */ }
  }
  private writeAtomic(path: string, text: string): void {
    const temporary = path + '.tmp';
    const file = fs.openSync(temporary, fs.OpenMode.CREATE | fs.OpenMode.READ_WRITE | fs.OpenMode.TRUNC);
    try {
      const count = fs.writeSync(file.fd, text);
      if (count !== text.length) throw new Error('Short clock metadata write');
      fs.fsyncSync(file.fd);
    } finally { fs.closeSync(file.fd); }
    fs.renameSync(temporary, path);
  }
  tick(force: boolean = false, checkpoint: boolean = false): void {
    if (this.nativeRecorder) return;
    if (!this.state || (!force && Date.now() < this.nextSample)) return;
    this.nextSample = Date.now() + 5000;
    try {
      this.state.anchors.push(inkNative.clockSample());
      if (this.state.anchors.length > 32) this.state.anchors.shift();
      this.anchorsSinceCheckpoint++;
      // Immutable, overlapping windows survive both the 32-entry memory ring
      // and a disconnected PC. Publish the count only after the window fsync.
      // Failure stays optional; a later gap is rejected by the PC, never filled.
      if (checkpoint || this.anchorsSinceCheckpoint >= 16) {
        const segment = this.path.replace('.clock.json', `.clock-${this.state.history_segments}.json`);
        this.writeAtomic(segment, JSON.stringify(this.state));
        this.state.history_segments++;
        this.anchorsSinceCheckpoint = 0;
      }
      this.writeAtomic(this.path, JSON.stringify(this.state));
    } catch (error) {
      // Optional calibration failure must not stop original pen capture/replay.
    }
  }
  stop(): void {
    if (this.nativeRecorder) {
      try { inkNative.stopClock(); } finally { this.nativeRecorder = false; }
      return;
    }
    // The PC waits for a durable final marker for both native and fallback
    // producers. A fallback without this marker would wait forever after stop.
    if (this.state) this.state.stopped = true;
    this.tick(true, true);
    this.state = undefined;
  }
}
