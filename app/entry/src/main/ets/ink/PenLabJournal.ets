import { fileIo as fs } from '@kit.CoreFileKit';
import { util } from '@kit.ArkTS';

export interface LabPoint {
  id: number;
  x: number;
  y: number;
  window_x: number;
  window_y: number;
  pressure?: number;
}

export interface LabHistory {
  event_timestamp: number;
  force: number;
  point: LabPoint;
}

export interface LabEvent {
  kind: string;
  seq?: number;
  session_id?: string;
  device_wall_ms?: number;
  event_timestamp?: number;
  action?: number;
  source_tool?: number;
  force?: number;
  scale?: number;
  scroll_y?: number;
  points?: LabPoint[];
  historical?: LabHistory[];
  detail?: string;
}

// Separate from the cross-app raw protocol. ArkUI events may be coalesced and
// canvas callbacks are observations, not confirmed final stroke/undo objects.
export class PenLabJournal {
  path: string = '';
  sessionId: string = util.generateRandomUUID();
  error: string = '';
  events: number = 0;
  private file?: fs.File;
  private queue: string[] = [];
  private pending?: Promise<void>;
  private timer: number = -1;
  private closed: boolean = false;
  private closeTask?: Promise<void>;
  private encoder: util.TextEncoder = new util.TextEncoder();

  start(filesDir: string): void {
    this.path = `${filesDir}/penlab-${this.sessionId}.jsonl`;
    if (fs.accessSync(this.path)) throw new Error('试写记录已存在，未覆盖');
    this.file = fs.openSync(this.path, fs.OpenMode.CREATE | fs.OpenMode.READ_WRITE);
    this.add({ kind: 'session_start', detail: 'penkit-lab-v1; event_timestamp unit uncalibrated; no cloud; not hinote' });
    this.timer = setInterval(() => { this.flush(); }, 250);
  }

  add(event: LabEvent): void {
    if (this.closed || this.error) return;
    if (this.queue.length >= 4096) {
      this.error = '记录缓冲已满，本次数据不完整，请保存后结束试写';
      this.queue.push(JSON.stringify({ kind: 'recording_incomplete', seq: this.events++,
        session_id: this.sessionId, device_wall_ms: Date.now(), detail: this.error }) + '\n');
      return;
    }
    event.seq = this.events++;
    event.session_id = this.sessionId;
    event.device_wall_ms = Date.now();
    this.queue.push(JSON.stringify(event) + '\n');
  }

  flush(): Promise<void> {
    if (this.pending) return this.pending;
    this.pending = this.drain().finally(() => { this.pending = undefined; });
    return this.pending;
  }

  private async drain(): Promise<void> {
    const file = this.file;
    if (!file) return;
    try {
      while (this.queue.length > 0) {
        const lines = this.queue.slice(0, 128);
        const bytes = this.encoder.encodeInto(lines.join(''));
        let written = 0;
        while (written < bytes.length) {
          const count = await fs.write(file.fd, bytes.buffer.slice(written));
          if (count <= 0) throw new Error('日志写入未完成');
          written += count;
        }
        this.queue.splice(0, lines.length);
      }
    } catch (error) {
      this.error = `记录失败：${error}`;
      // A partial write cannot safely be retried from the beginning.
      this.closed = true;
    }
  }

  async sync(): Promise<void> {
    await this.flush();
    if (this.file) await fs.fsync(this.file.fd);
    if (this.error) throw new Error(this.error);
  }

  close(): Promise<void> {
    if (!this.closeTask) this.closeTask = this.finish();
    return this.closeTask;
  }

  private async finish(): Promise<void> {
    if (this.timer >= 0) clearInterval(this.timer);
    this.timer = -1;
    this.add({ kind: 'session_end', detail: this.error || 'page_closed' });
    this.closed = true;
    try { await this.sync(); }
    finally {
      if (this.file) fs.closeSync(this.file);
      this.file = undefined;
    }
  }
}
