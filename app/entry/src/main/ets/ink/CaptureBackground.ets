import { common, wantAgent, WantAgent } from '@kit.AbilityKit';
import { backgroundTaskManager } from '@kit.BackgroundTasksKit';
import { fileIo as fs } from '@kit.CoreFileKit';
import { util } from '@kit.ArkTS';
import { BusinessError } from '@kit.BasicServicesKit';

interface BackgroundEvent { kind: string; at_ms: number; task_id: number; code: number; detail: string; }

class BackgroundTaskError extends Error {
  code: number;
  constructor(code: number, message: string) {
    super(message);
    this.code = code;
  }
}

// The screen-recording service must already be user-authorized. This requests
// its matching official continuous task, never audio playback or timer renewal.
export class CaptureBackground {
  private context: common.UIAbilityContext | undefined = undefined;
  private agent: WantAgent | undefined = undefined;
  private pending: Promise<void> | undefined = undefined;
  private taskId: number = -1;
  private events: Array<BackgroundEvent> = [];
  private cancelListener: ((info: backgroundTaskManager.ContinuousTaskCancelInfo) => void) | undefined = undefined;

  private note(kind: string, code: number = 0, detail: string = ''): void {
    this.events.push({ kind: kind, at_ms: Date.now(), task_id: this.taskId, code: code, detail: detail.slice(0, 240) });
    if (this.events.length > 32) this.events.shift();
    if (!this.context) return;
    try {
      const path = `${this.context.filesDir}/capture-background.json`;
      const file = fs.openSync(path + '.tmp', fs.OpenMode.CREATE | fs.OpenMode.READ_WRITE | fs.OpenMode.TRUNC);
      try {
        const bytes = new util.TextEncoder().encodeInto(JSON.stringify(this.events));
        let offset = 0;
        while (offset < bytes.byteLength) {
          const count = fs.writeSync(file.fd, bytes.buffer.slice(offset));
          if (count <= 0) throw new Error('Short background diagnostic write');
          offset += count;
        }
        fs.fsyncSync(file.fd);
      } finally { fs.closeSync(file.fd); }
      fs.renameSync(path + '.tmp', path);
    } catch (error) { /* Optional diagnostics cannot break raw capture. */ }
  }

  start(context: common.UIAbilityContext, onCancel: (reason: number) => void): Promise<void> {
    if (this.pending || this.taskId >= 0) return Promise.reject(new Error('Background task still active'));
    this.context = context;
    this.pending = this.acquire(context, onCancel);
    return this.pending;
  }

  private async acquire(context: common.UIAbilityContext, onCancel: (reason: number) => void): Promise<void> {
    this.note('request');
    let stage: string = 'want_agent';
    try {
      // Only a deliberate user tap on the system task notification opens UI.
      // Merely constructing the WantAgent does not launch an Ability.
      this.agent = await wantAgent.getWantAgent({
        wants: [{ bundleName: 'com.example.inkbridge', abilityName: 'EntryAbility' }],
        requestCode: 19021, actionType: wantAgent.OperationType.START_ABILITY,
        actionFlags: [wantAgent.WantAgentFlags.UPDATE_PRESENT_FLAG]
      });
      stage = 'request_configuration';
      const request = new backgroundTaskManager.ContinuousTaskRequest();
      request.backgroundTaskModes = [backgroundTaskManager.BackgroundTaskMode.MODE_AV_PLAYBACK_AND_RECORD];
      request.backgroundTaskSubmodes = [backgroundTaskManager.BackgroundTaskSubmode.SUBMODE_SCREEN_RECORD_NORMAL_NOTIFICATION];
      request.wantAgent = this.agent;
      // Request the exact screen-recording mode directly. The separate legacy
      // capability query converts only the parent mode, whereas the real start
      // resolves the screen-record submode before validating permissions and
      // configuration. The service remains the authority for the actual grant.
      this.cancelListener = (info: backgroundTaskManager.ContinuousTaskCancelInfo) => {
        if (info.id !== this.taskId || this.taskId < 0) return;
        this.note('system_cancel', info.reason);
        // The service already removed this task. A second cancellation can
        // fail with "task not found" and hide the original interruption.
        this.taskId = -1;
        onCancel(info.reason);
      };
      backgroundTaskManager.on('continuousTaskCancel', this.cancelListener);
      stage = 'start_background';
      this.note('start_submitted');
      const result = await backgroundTaskManager.startBackgroundRunning(context, request);
      stage = 'task_id';
      if (result.continuousTaskId === undefined || result.continuousTaskId < 0) {
        // Do not leak a granted task if firmware omits the API21 task ID.
        await backgroundTaskManager.stopBackgroundRunning(context);
        throw new Error('Screen-recording task ID missing');
      }
      this.taskId = result.continuousTaskId;
      this.note('granted');
    } catch (error) {
      const problem = error as BusinessError;
      this.note('request_failed', problem.code ?? -1, `${stage}: ${problem.message}`);
      throw new BackgroundTaskError(problem.code ?? -1, problem.message || 'Screen-recording background request failed');
    }
  }

  async stop(): Promise<void> {
    // A stop during an in-flight grant must wait, then release that exact task.
    // The controller's generation gate prevents subsequently starting input.
    if (this.pending) {
      try { await this.pending; } catch (error) { /* Still clean up agent/listener. */ }
      this.pending = undefined;
    }
    if (this.cancelListener) {
      try { backgroundTaskManager.off('continuousTaskCancel', this.cancelListener); } catch (error) { /* Already removed. */ }
      this.cancelListener = undefined;
    }
    if (this.context && this.taskId >= 0) {
      try {
        await backgroundTaskManager.stopBackgroundRunning(this.context, this.taskId);
        this.note('released');
        this.taskId = -1;
      } catch (error) {
        this.note('release_failed', (error as BusinessError).code ?? -1);
        throw new Error('Screen-recording background release failed');
      }
    }
    if (this.agent) {
      try { await wantAgent.cancel(this.agent); } catch (error) { /* Already cancelled. */ }
      this.agent = undefined;
    }
    this.context = undefined;
  }
}
