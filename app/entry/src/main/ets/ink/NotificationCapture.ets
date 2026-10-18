import { common, wantAgent, WantAgent } from '@kit.AbilityKit';
import { notificationManager } from '@kit.NotificationKit';
import { commonEventManager, BusinessError } from '@kit.BasicServicesKit';
import { util } from '@kit.ArkTS';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { fileIo as fs } from '@kit.CoreFileKit';
import { backgroundTaskManager } from '@kit.BackgroundTasksKit';
import { captureController } from './CaptureController';

interface ControlEvent { kind: string; at_ms: number; detail: string; }
// The idle action opens the capture page only on a deliberate user tap.
// Authorization/start is a separate foreground button; Stop stays an event.
class NotificationCapture {
  message: string = '';
  enabled: boolean = false;
  private context: common.UIAbilityContext | undefined = undefined;
  private subscriber: commonEventManager.CommonEventSubscriber | undefined = undefined;
  private startAgent: WantAgent | undefined = undefined;
  private stopAgent: WantAgent | undefined = undefined;
  private timer: number = -1;
  private busy: boolean = false;
  private publishing: boolean = false;
  private lastState: string = '';
  private generation: number = 0;
  private readonly id: number = 19017;
  private delayId: number = -1;
  private armUntil: number = 0;
  private expiryTimer: number = -1;
  private pingTimer: number = -1;
  private pingResolve: (() => void) | undefined = undefined;
  private diagnosticPath: string = '';
  private events: Array<ControlEvent> = [];

  private note(kind: string, detail: string = ''): void {
    this.events.push({ kind: kind, at_ms: Date.now(), detail: detail.slice(0, 240) });
    if (this.events.length > 16) this.events.shift();
    if (!this.diagnosticPath) return;
    try {
      const temporary = this.diagnosticPath + '.tmp';
      const file = fs.openSync(temporary, fs.OpenMode.CREATE | fs.OpenMode.READ_WRITE | fs.OpenMode.TRUNC);
      try {
        const bytes = new util.TextEncoder().encodeInto(JSON.stringify(this.events));
        let written = 0;
        while (written < bytes.byteLength) {
          const count = fs.writeSync(file.fd, bytes.buffer.slice(written));
          if (count <= 0) throw new Error('Short control diagnostics write');
          written += count;
        }
      } finally { fs.closeSync(file.fd); }
      fs.renameSync(temporary, this.diagnosticPath);
    } catch (error) { /* Optional bounded diagnostics cannot stop capture. */ }
  }

  async enable(context: common.UIAbilityContext): Promise<void> {
    if (this.busy || this.enabled) return;
    this.busy = true;
    const generation = ++this.generation;
    this.context = context;
    this.diagnosticPath = `${context.filesDir}/capture-controls.json`;
    this.note('enable_requested');
    try {
      if (!await notificationManager.isNotificationEnabled()) {
        await notificationManager.requestEnableNotification(context);
      }
      if (generation !== this.generation) return;
      if (!await notificationManager.isNotificationEnabled()) throw new Error('Notification disabled');
      // New actions on every enable prevent old queued notifications from
      // starting capture after the process is thawed or controls are re-armed.
      const nonce = util.generateRandomUUID(true);
      const stop = `com.example.inkbridge.capture.STOP.${nonce}`;
      const ping = `com.example.inkbridge.capture.PING.${nonce}`;
      this.subscriber = await commonEventManager.createSubscriber({ events: [stop, ping] });
      // Promise API acknowledges subscription and avoids treating a successful
      // BusinessError {code: 0} callback argument as an error object.
      await commonEventManager.subscribeToEvent(this.subscriber, (data) => {
        if (generation !== this.generation) return;
        if (data.event === ping) {
          this.note('event_route_verified');
          if (this.pingResolve) this.pingResolve();
          return;
        }
        if (!this.enabled || !this.context) return;
        this.note('control_event_received', data.event === stop ? 'stop' : 'unknown');
        if (data.event === stop) {
          this.note('stop_received');
          hilog.info(0x0001, 'InkBridge', 'Notification stop requested by user');
          captureController.stopCapture();
        }
      });
      this.note('subscribed');
      this.startAgent = await wantAgent.getWantAgent({ wants: [{ bundleName: 'com.example.inkbridge', abilityName: 'EntryAbility' }], requestCode: 19018,
        actionType: wantAgent.OperationType.START_ABILITY,
        actionFlags: [wantAgent.WantAgentFlags.UPDATE_PRESENT_FLAG] });
      this.stopAgent = await wantAgent.getWantAgent({ wants: [{ bundleName: 'com.example.inkbridge', action: stop }], requestCode: 19019,
        actionType: wantAgent.OperationType.SEND_COMMON_EVENT,
        actionFlags: [wantAgent.WantAgentFlags.UPDATE_PRESENT_FLAG] });
      const pingAgent = await wantAgent.getWantAgent({ wants: [{ bundleName: 'com.example.inkbridge', action: ping }], requestCode: 19020,
        actionType: wantAgent.OperationType.SEND_COMMON_EVENT,
        actionFlags: [wantAgent.WantAgentFlags.UPDATE_PRESENT_FLAG] });
      try {
        await new Promise<void>((resolve, reject) => {
          this.pingResolve = resolve;
          this.pingTimer = setTimeout(() => { reject(new Error('Event route timeout')); }, 3000);
          wantAgent.trigger(pingAgent, { code: 0 }, (error) => {
            if (error && error.code !== 0) reject(new Error(`Event route ${error.code}`));
          });
        });
      } finally {
        this.pingResolve = undefined;
        if (this.pingTimer >= 0) clearTimeout(this.pingTimer);
        this.pingTimer = -1;
        wantAgent.cancel(pingAgent).catch(() => {});
      }
      if (generation !== this.generation) { this.disable(); return; }
      if (!captureController.running && !captureController.authorizing) {
        const delay = backgroundTaskManager.requestSuspendDelay('完成由你发起的采集启动与系统授权', () => {
          this.note('suspend_delay_expired');
          this.releaseDelay();
          this.expire();
        });
        this.delayId = delay.requestId;
        // Reserve the controller's full 60s authorization timeout before the
        // OS grant expires, even when Start is clicked at the last moment.
        if (delay.requestId < 0 || delay.actualDelayTime < 66000) throw new Error('Insufficient background window');
        // This is a finite, user-initiated transition. Never renew it in a loop.
        const duration = Math.min(90000, delay.actualDelayTime - 61000);
        this.armUntil = Date.now() + duration;
        this.expiryTimer = setTimeout(() => { this.expire(); }, duration);
        this.note('suspend_delay_granted', `request=${delay.requestId}; granted_ms=${delay.actualDelayTime}; armed_ms=${duration}`);
      }
      this.enabled = true;
      this.lastState = '';
      await this.refresh();
      if (this.enabled) {
        this.timer = setInterval(() => { this.refresh(); }, 500);
        this.message = captureController.running ? '通知栏可停止当前记录。' :
          '请在本页授权并开始记录，等显示“正在记录”后回华为笔记。通知可打开本页或停止记录。';
      }
    } catch (error) {
      const problem = error as BusinessError;
      this.note('enable_failed', `${problem.code}: ${problem.message}`);
      this.disable();
      this.message = `通知栏控制未开启（${problem.code ?? '事件或短时后台不可用'}）。请使用本页“授权并开始记录”。`;
    } finally { this.busy = false; }
  }

  private async refresh(): Promise<void> {
    if (!this.enabled || this.publishing || !this.startAgent || !this.stopAgent) return;
    if (captureController.running && (this.armUntil > 0 || this.delayId >= 0)) {
      this.armUntil = 0;
      this.releaseDelay();
      if (this.expiryTimer >= 0) clearTimeout(this.expiryTimer);
      this.expiryTimer = -1;
    }
    if (!captureController.running && !captureController.authorizing && !captureController.stopping && Date.now() >= this.armUntil) {
      this.expire();
      return;
    }
    const state = captureController.stopping ? 'stopping' : captureController.authorizing ? 'authorizing' :
      captureController.running ? 'running' : 'idle';
    const stateKey = state + ':' + captureController.message;
    if (stateKey === this.lastState) return;
    this.publishing = true;
    try {
      const active = state !== 'idle';
      const agent = active ? this.stopAgent : this.startAgent;
      await notificationManager.publish({
        id: this.id,
        notificationSlotType: notificationManager.SlotType.SERVICE_INFORMATION,
        content: { notificationContentType: notificationManager.ContentType.NOTIFICATION_CONTENT_BASIC_TEXT,
          normal: { title: '笔迹采集控制', text: state === 'running' ? '正在记录；点停止结束并同步。' :
            state === 'authorizing' ? '等待系统授权；可取消。' : state === 'stopping' ? '正在结束并保存。' :
              '点打开采集页，在前台授权并开始；显示正在记录后再回笔记。入口90秒内有效。' } },
        actionButtons: [{ title: active ? '停止记录／取消' : '打开采集页', wantAgent: agent }],
        wantAgent: agent, tapDismissed: false, isOngoing: true
      });
      if (this.enabled) {
        this.lastState = stateKey;
        this.note('published_' + state, captureController.message);
      }
      else await notificationManager.cancel(this.id);
    } catch (error) {
      const problem = error as BusinessError;
      this.note('publish_failed', `${problem.code}`);
      this.disable();
      this.message = `通知栏发布失败（${problem.code}）；可回到应用使用记录按钮。`;
    } finally { this.publishing = false; }
  }

  private releaseDelay(): void {
    if (this.delayId < 0) return;
    const id = this.delayId;
    this.delayId = -1;
    try { backgroundTaskManager.cancelSuspendDelay(id); } catch (error) { /* Already expired. */ }
  }

  private expire(): void {
    this.armUntil = 0;
    if (this.expiryTimer >= 0) clearTimeout(this.expiryTimer);
    this.expiryTimer = -1;
    if (captureController.running || captureController.authorizing || captureController.stopping) return;
    this.releaseDelay();
    this.disable();
    this.message = '通知启动窗口已过期；请回本页重新开启，或直接授权并开始记录。';
  }

  disable(): void {
    this.note('disabled');
    ++this.generation;
    this.enabled = false;
    this.armUntil = 0;
    this.releaseDelay();
    if (this.expiryTimer >= 0) clearTimeout(this.expiryTimer);
    this.expiryTimer = -1;
    if (this.timer >= 0) clearInterval(this.timer);
    this.timer = -1;
    const subscriber = this.subscriber;
    this.subscriber = undefined;
    if (subscriber) {
      try { commonEventManager.unsubscribe(subscriber); } catch (error) { /* System already removed it. */ }
    }
    const startAgent = this.startAgent;
    const stopAgent = this.stopAgent;
    this.startAgent = undefined;
    this.stopAgent = undefined;
    if (startAgent) wantAgent.cancel(startAgent).catch(() => {});
    if (stopAgent) wantAgent.cancel(stopAgent).catch(() => {});
    this.context = undefined;
    notificationManager.cancel(this.id).catch(() => {});
    this.message = '通知栏控制已关闭；当前采集状态不变。';
  }
}

export const notificationCapture = new NotificationCapture();
