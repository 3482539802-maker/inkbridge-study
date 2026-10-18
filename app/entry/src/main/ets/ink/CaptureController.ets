import { abilityAccessCtrl, common } from '@kit.AbilityKit';
import { fileIo as fs } from '@kit.CoreFileKit';
import { socket } from '@kit.NetworkKit';
import { util } from '@kit.ArkTS';
import { BusinessError } from '@kit.BasicServicesKit';
import { hilog } from '@kit.PerformanceAnalysisKit';
import inkNative, { InkPoint } from 'libink_native.so';
import { CaptureClock } from './CaptureClock';
import { CaptureBackground } from './CaptureBackground';

interface SessionPoint extends InkPoint { session_id: string; }
export interface InkSession {
  session_id: string;
  started_at: number;
  ended_at: number;
  state: string;
  stop_reason: string;
  point_count: number;
  dropped_count: number;
  byte_length: number;
  synced: boolean;
  loss_accounting_complete: boolean;
}
interface HelloFrame { type: string; protocol: number; session_id: string; started_at: number; }
interface BatchFrame { type: string; session_id: string; offset: number; data: string; }
interface EndFrame {
  type: string; session_id: string; byte_length: number; state: string;
  ended_at: number; stop_reason: string; point_count: number; dropped_count: number;
  loss_accounting_complete: boolean;
}
interface Ack { type: string; session_id: string; offset: number; complete: boolean; }

// Capture and replay live independently of the page. Leaving Huawei Notes alone
// is essential: a socket failure must never stop the input monitor.
class CaptureController {
  running: boolean = false;
  authorizing: boolean = false;
  stopping: boolean = false;
  connected: boolean = false;
  message: string = '在本页开始并确认系统授权，等显示“正在记录”后再回华为笔记书写。';
  syncMessage: string = '本地文件优先；电脑离线时保留记录，连接恢复后自动回补。';
  points: number = 0;
  penPoints: number = 0;
  dropped: number = 0;
  filePath: string = '';
  sessions: Array<InkSession> = [];
  limitMs: number = 0;
  private directory: string = '';
  private active: InkSession | undefined = undefined;
  private file: fs.File | undefined = undefined;
  private buffer: Array<SessionPoint> = [];
  private timer: number = -1;
  private authorizationTimer: number = -1;
  private screenGeneration: number = 0;
  private localFailed: boolean = false;
  private tcp: socket.TCPSocket | undefined = undefined;
  private syncing: boolean = false;
  private disposed: boolean = false;
  private retryAt: number = 0;
  private retryDelay: number = 1000;
  private incoming: string = '';
  private resolveAck: ((ack: Ack) => void) | undefined = undefined;
  private rejectAck: ((error: Error) => void) | undefined = undefined;
  private clock: CaptureClock = new CaptureClock();
  private background: CaptureBackground = new CaptureBackground();
  private captureStarting: boolean = false;
  private foreground: boolean = false;
  private screenReady: boolean = false;
  private startContext: common.UIAbilityContext | undefined = undefined;

  setForeground(value: boolean): void {
    this.foreground = value;
    if (value) this.beginWhenForeground();
  }

  private beginWhenForeground(): void {
    if (!this.authorizing || !this.screenReady || this.captureStarting || !this.startContext) return;
    if (!this.foreground) {
      this.message = '屏幕已授权，请回到采集页完成启动；显示“正在记录”后再回华为笔记。';
      return;
    }
    this.captureStarting = true;
    this.clearAuthorizationTimer();
    this.beginCapture(this.startContext, this.screenGeneration);
  }

  initialize(context: common.UIAbilityContext): void {
    if (this.timer >= 0) return;
    this.disposed = false;
    this.directory = context.filesDir;
    this.sessions = [];
    fs.listFileSync(this.directory).filter((name: string) => name.endsWith('.session.json'))
      .sort().forEach((name: string) => {
        try {
          const session = JSON.parse(fs.readTextSync(`${this.directory}/${name}`)) as InkSession;
          if (!/^[A-Za-z0-9_-]{1,80}$/.test(session.session_id)) return;
          if (session.state === 'recording') {
            // Process death is an interruption, never an automatic new screen grant.
            this.recoverTail(session);
            session.state = 'interrupted';
            session.ended_at = Date.now();
            session.stop_reason = 'process_interrupted';
            session.synced = false;
            session.loss_accounting_complete = false;
            this.save(session);
          }
          this.sessions.push(session);
        } catch (error) {
          this.message = '部分旧会话无法读取；原始文件已保留，请检查设备日志。';
        }
      });
    this.timer = setInterval(() => {
      try { this.flush(); } catch (error) { this.stopCapture('local_write_failed'); }
      if (this.running) {
        this.clock.tick();
        this.dropped = inkNative.status().dropped_count;
        if (this.limitMs > 0 && this.active && Date.now() - this.active.started_at >= this.limitMs) {
          this.stopCapture('time_limit');
        }
      }
      this.sync();
    }, 100);
  }

  private rawPath(session: InkSession): string { return `${this.directory}/ink-${session.session_id}.jsonl`; }

  private writeAll(fd: number, text: string): number {
    const bytes = new util.TextEncoder().encodeInto(text);
    let written = 0;
    while (written < bytes.byteLength) {
      const count = fs.writeSync(fd, bytes.buffer.slice(written));
      if (count <= 0) throw new Error('Short local write');
      written += count;
    }
    return written;
  }

  private save(session: InkSession): void {
    const path = `${this.directory}/ink-${session.session_id}.session.json`;
    const temporary = `${path}.tmp`;
    const file = fs.openSync(temporary, fs.OpenMode.CREATE | fs.OpenMode.READ_WRITE | fs.OpenMode.TRUNC);
    try {
      this.writeAll(file.fd, JSON.stringify(session));
      fs.fsyncSync(file.fd);
    } finally { fs.closeSync(file.fd); }
    fs.renameSync(temporary, path);
  }

  private readChunk(session: InkSession, offset: number): string {
    const file = fs.openSync(this.rawPath(session), fs.OpenMode.READ_ONLY);
    try {
      const buffer = new ArrayBuffer(32768);
      const size = fs.readSync(file.fd, buffer, { offset: offset, length: buffer.byteLength });
      // Point records contain ASCII keys, numbers and a UUID. Offsets are bytes.
      const text = util.TextDecoder.create('utf-8').decodeToString(new Uint8Array(buffer, 0, size));
      return text.slice(0, text.lastIndexOf('\n') + 1);
    } finally { fs.closeSync(file.fd); }
  }

  private recoverTail(session: InkSession): void {
    const path = this.rawPath(session);
    const size = fs.statSync(path).size;
    if (size === 0) { session.byte_length = 0; session.point_count = 0; return; }
    const file = fs.openSync(path, fs.OpenMode.READ_WRITE);
    try {
      const start = Math.max(0, size - 32768);
      const bytes = new ArrayBuffer(size - start);
      const count = fs.readSync(file.fd, bytes, { offset: start, length: bytes.byteLength });
      const text = util.TextDecoder.create('utf-8').decodeToString(new Uint8Array(bytes, 0, count));
      const boundary = text.lastIndexOf('\n') + 1;
      if (size > 0 && boundary === 0) throw new Error('No complete journal boundary');
      session.byte_length = start + boundary;
      fs.truncateSync(file.fd, session.byte_length);
      fs.fsyncSync(file.fd);
    } finally { fs.closeSync(file.fd); }
    // Count complete saved records; native seq can include queue drops.
    session.point_count = 0;
    let offset = 0;
    while (offset < session.byte_length) {
      const chunk = this.readChunk(session, offset);
      if (!chunk) throw new Error('Corrupt local journal');
      session.point_count += chunk.split('\n').length - 1;
      offset += chunk.length;
    }
  }

  async start(context: common.UIAbilityContext): Promise<void> {
    if (this.running || this.authorizing || this.stopping) return;
    if (!this.foreground) {
      this.message = '请打开采集页，在前台点击“授权并开始记录”；显示“正在记录”后再回笔记。';
      return;
    }
    this.initialize(context);
    this.authorizing = true;
    this.screenReady = false;
    this.startContext = context;
    const generation = ++this.screenGeneration;
    this.message = '请亲自确认系统屏幕采集授权；声音关闭，原始笔事件单独保存，已启用的定位测试可按需临时读取画面。';
    try {
      const code = inkNative.beginScreen((state: number) => {
        if (generation !== this.screenGeneration) return;
        hilog.info(0x0001, 'InkBridge', 'screen_state=%{public}d', state);
        if (state === 0 && this.authorizing && !this.captureStarting) {
          this.screenReady = true;
          this.beginWhenForeground();
        } else if (state < 0 || [1, 2, 3, 4, 10].includes(state)) {
          this.stopCapture(`screen_state_${state}`);
        }
      }, context.filesDir);
      if (code !== 0) { await this.stopCapture(`screen_start_${code}`); return; }
      if (this.authorizing) {
        this.authorizationTimer = setTimeout(() => {
          if (generation === this.screenGeneration && this.authorizing) this.stopCapture('authorization_timeout');
        }, 60000);
      }
    } catch (error) {
      await this.stopCapture('screen_start_failed');
    }
  }

  private async beginCapture(context: common.UIAbilityContext, generation: number): Promise<void> {
    try {
      // AVScreenCapture is STARTED and user-authorized before this request.
      await this.background.start(context, (reason: number) => {
        this.stopCapture(`background_cancel_${reason}`);
      });
      if (generation !== this.screenGeneration || !this.authorizing || this.stopping) return;
      const permission = abilityAccessCtrl.createAtManager().checkAccessTokenSync(
        context.applicationInfo.accessTokenId, 'ohos.permission.INPUT_MONITORING');
      hilog.info(0x0001, 'InkBridge', 'input_monitoring_grant=%{public}d', permission);
      const session: InkSession = {
        session_id: util.generateRandomUUID(), started_at: Date.now(), ended_at: 0,
        state: 'recording', stop_reason: '', point_count: 0, dropped_count: 0, byte_length: 0, synced: false,
        loss_accounting_complete: true
      };
      this.active = session;
      this.filePath = this.rawPath(session);
      this.file = fs.openSync(this.filePath, fs.OpenMode.CREATE | fs.OpenMode.READ_WRITE);
      this.save(session);
      this.sessions.push(session);
      this.clock.start(this.directory, session.session_id);
      this.buffer = [];
      this.localFailed = false;
      this.points = this.penPoints = this.dropped = 0;
      const code = inkNative.start((point: InkPoint) => {
        this.clock.observe(point.action_time, point.seq);
        const stored: SessionPoint = {
          type: point.type, seq: point.seq, action: point.action, pointer_id: point.pointer_id,
          x: point.x, y: point.y, action_time: point.action_time, down_time: point.down_time,
          pressure: point.pressure, tool_type: point.tool_type, window_id: point.window_id,
          display_id: point.display_id, session_id: session.session_id
        };
        this.points++;
        if ([1, 3, 4, 5].includes(point.tool_type)) this.penPoints++;
        this.buffer.push(stored);
        if (this.buffer.length >= 32) {
          try { this.flush(); } catch (error) { this.stopCapture('local_write_failed'); }
        }
      });
      if (code !== 0) { await this.stopCapture(`monitor_start_${code}`); return; }
      this.running = true;
      this.authorizing = false;
      this.captureStarting = false;
      this.message = '正在记录。请继续在华为笔记里书写，写完回来停止；断网不影响本地记录。';
      // UI-owned timers may pause in the background while native callbacks
      // continue. Publish the session immediately, without waiting for a tick.
      this.retry();
    } catch (error) {
      const problem = error as BusinessError;
      hilog.error(0x0001, 'InkBridge', 'capture_start_failed code=%{public}d', problem.code);
      if (generation === this.screenGeneration) {
        await this.stopCapture(`capture_start_failed_${problem.code ?? -1}`);
        if (problem.code === 9800005 && problem.message.includes('not allowed to be applied on background')) {
          this.message = '启动未完成：请留在采集页重新授权，等显示“正在记录”后再回华为笔记。';
        }
      }
    }
  }

  private flush(): void {
    if (this.localFailed || !this.file || !this.active || this.buffer.length === 0) return;
    let text = '';
    this.buffer.forEach((point: SessionPoint) => { text += JSON.stringify(point) + '\n'; });
    try {
      const bytes = this.writeAll(this.file.fd, text);
      fs.fsyncSync(this.file.fd);
      this.active.byte_length += bytes;
      this.active.point_count += this.buffer.length;
      this.buffer = [];
      // Event-driven replay also works when the page's 100ms timer is paused.
      // sync() retains the existing single-flight and retry throttles.
      this.sync();
      // Bound a session's local storage without buffering a whole recording in RAM.
      if (this.active.byte_length >= 512 * 1024 * 1024 && !this.stopping) this.stopCapture('storage_limit');
    } catch (error) {
      this.localFailed = true;
      this.buffer = [];
      fs.truncateSync(this.file.fd, this.active.byte_length);
      throw new Error('Local journal write failed');
    }
  }

  private clearAuthorizationTimer(): void {
    if (this.authorizationTimer >= 0) clearTimeout(this.authorizationTimer);
    this.authorizationTimer = -1;
  }

  async stopCapture(reason: string = 'user_stop'): Promise<void> {
    if (this.stopping) return;
    this.stopping = true;
    this.authorizing = false;
    this.captureStarting = false;
    this.screenReady = false;
    this.startContext = undefined;
    ++this.screenGeneration;
    this.clearAuthorizationTimer();
    let finalReason = reason;
    try {
      const code = await inkNative.stop();
      if (code !== 0) {
        finalReason = `monitor_stop_${code}`;
        if (this.active) this.active.loss_accounting_complete = false;
      }
      this.flush(); // Native stop resolves only after queued points have drained.
      this.dropped = inkNative.status().dropped_count;
    } catch (error) { finalReason = 'local_or_stop_failed'; }
    finally {
      try { inkNative.endScreen(); } catch (error) { finalReason = 'screen_release_failed'; }
      try { await this.background.stop(); } catch (error) { finalReason = 'background_release_failed'; }
      try { this.clock.stop(); }
      catch (error) {
        // Optional geometry timing must not strand the canonical raw file or
        // prevent its completion ACK. Missing final clock stays unknown on PC.
        hilog.error(0x0001, 'InkBridge', 'clock_stop_failed_raw_preserved');
      }
      if (this.file) {
        try { fs.closeSync(this.file.fd); } catch (error) { finalReason = 'file_close_failed'; }
        this.file = undefined;
      }
      if (this.active) {
        if (this.localFailed || finalReason === 'local_or_stop_failed') this.active.loss_accounting_complete = false;
        this.active.state = ['user_stop', 'time_limit', 'storage_limit'].includes(finalReason) ? 'completed' : 'interrupted';
        this.active.ended_at = Date.now();
        this.active.stop_reason = finalReason;
        this.active.dropped_count = this.dropped;
        try { this.save(this.active); } catch (error) { finalReason = 'metadata_save_failed'; }
      }
      this.active = undefined;
      this.running = false;
      this.stopping = false;
      this.message = finalReason === 'user_stop' ? '已停止记录，本地已保存；电脑同步会继续。' : `记录已结束：${finalReason}。原始文件已保留。`;
      this.retry();
    }
  }

  private disconnect(): void {
    this.connected = false;
    const tcp = this.tcp;
    this.tcp = undefined;
    if (this.rejectAck) this.rejectAck(new Error('Disconnected'));
    this.resolveAck = undefined;
    this.rejectAck = undefined;
    if (tcp) tcp.close().catch(() => {});
  }

  private async connect(): Promise<void> {
    if (this.connected) return;
    this.disconnect();
    const tcp = socket.constructTCPSocketInstance();
    this.tcp = tcp;
    this.incoming = '';
    tcp.on('close', () => { if (this.tcp === tcp) this.disconnect(); });
    tcp.on('error', () => { if (this.tcp === tcp) this.disconnect(); });
    tcp.on('message', (info: socket.SocketMessageInfo) => {
      if (this.tcp !== tcp) return;
      this.incoming += util.TextDecoder.create('utf-8').decodeToString(new Uint8Array(info.message));
      if (this.incoming.length > 8192) { this.disconnect(); return; }
      let boundary = this.incoming.indexOf('\n');
      while (boundary >= 0) {
        const line = this.incoming.slice(0, boundary);
        this.incoming = this.incoming.slice(boundary + 1);
        try {
          const ack = JSON.parse(line) as Ack;
          if (ack.type !== 'ack' || !Number.isInteger(ack.offset) || ack.offset < 0) {
            this.disconnect(); return;
          }
          if (this.resolveAck) this.resolveAck(ack);
        } catch (error) { this.disconnect(); return; }
        boundary = this.incoming.indexOf('\n');
      }
    });
    await tcp.connect({ address: { address: '127.0.0.1', port: 18765, family: 1 }, timeout: 3000 });
    if (this.tcp !== tcp || this.disposed) throw new Error('Stale connection');
    this.connected = true;
  }

  private exchange(text: string): Promise<Ack> {
    return new Promise<Ack>((resolve, reject) => {
      const timer = setTimeout(() => { this.disconnect(); reject(new Error('ACK timeout')); }, 6000);
      this.resolveAck = (ack: Ack) => {
        clearTimeout(timer); this.resolveAck = undefined; this.rejectAck = undefined; resolve(ack);
      };
      this.rejectAck = (error: Error) => {
        clearTimeout(timer); this.resolveAck = undefined; this.rejectAck = undefined; reject(error);
      };
      if (!this.tcp) { this.disconnect(); reject(new Error('No socket')); return; }
      this.tcp.send({ data: text + '\n' }).catch(() => { this.disconnect(); });
    });
  }

  private checkAck(ack: Ack, session: InkSession, expected: number = -1): void {
    if (ack.session_id !== session.session_id || ack.offset > session.byte_length ||
      (expected >= 0 && ack.offset !== expected)) throw new Error('Invalid ACK boundary');
  }

  async sync(): Promise<void> {
    if (this.syncing || this.disposed || Date.now() < this.retryAt) return;
    const session = this.sessions.find((item: InkSession) => !item.synced);
    if (!session) return;
    this.syncing = true;
    let continueReplay: boolean = false;
    try {
      await this.connect();
      const hello: HelloFrame = { type: 'session', protocol: 2, session_id: session.session_id, started_at: session.started_at };
      let ack = await this.exchange(JSON.stringify(hello));
      this.checkAck(ack, session);
      // Limit work per turn; the local file is the queue, never a growing RAM list.
      for (let batch = 0; batch < 8 && ack.offset < session.byte_length; batch++) {
        const data = this.readChunk(session, ack.offset);
        if (!data) throw new Error('Incomplete journal boundary');
        const frame: BatchFrame = { type: 'batch', session_id: session.session_id, offset: ack.offset, data: data };
        const expected = ack.offset + data.length;
        ack = await this.exchange(JSON.stringify(frame));
        this.checkAck(ack, session, expected);
      }
      if (session.state !== 'recording' && ack.offset === session.byte_length) {
        const end: EndFrame = {
          type: 'session_end', session_id: session.session_id, byte_length: session.byte_length,
          state: session.state, ended_at: session.ended_at, stop_reason: session.stop_reason,
          point_count: session.point_count, dropped_count: session.dropped_count,
          loss_accounting_complete: session.loss_accounting_complete
        };
        ack = await this.exchange(JSON.stringify(end));
        this.checkAck(ack, session, session.byte_length);
        if (!ack.complete) throw new Error('Missing completion ACK');
        session.synced = true;
        try { this.save(session); }
        catch (error) {
          // The completion ACK is durable on PC, but our local receipt is not.
          // Keep the session retryable until its synced marker is persisted.
          session.synced = false;
          throw new Error('Local sync receipt save failed');
        }
      }
      this.syncMessage = `电脑已确认 ${ack.offset} / ${session.byte_length} 字节 · ${session.session_id.slice(0, 8)}`;
      this.retryDelay = 1000;
      // A stopped backlog can exceed eight batches. Drain successful backlog
      // through socket completions; do not depend on another UI timer/input.
      continueReplay = session.synced || ack.offset < session.byte_length;
      this.retryAt = continueReplay ? 0 : Date.now() + 500;
    } catch (error) {
      this.disconnect();
      this.syncMessage = '电脑离线或确认失败；原始记录保留，正在自动重连回补。';
      this.retryAt = Date.now() + this.retryDelay;
      this.retryDelay = Math.min(this.retryDelay * 2, 30000);
    } finally { this.syncing = false; }
    if (continueReplay) this.sync();
  }

  retry(): void { this.retryAt = 0; this.sync(); }

  resyncAll(): void {
    this.sessions.forEach((session: InkSession) => {
      session.synced = false;
      try { this.save(session); } catch (error) { this.syncMessage = '同步标记保存失败，原始文件仍保留。'; }
    });
    this.retry();
  }

  async shutdown(): Promise<void> {
    this.disposed = true;
    if (this.timer >= 0) clearInterval(this.timer);
    this.timer = -1;
    this.disconnect();
    await this.stopCapture('ability_destroyed');
  }
}

export const captureController = new CaptureController();
