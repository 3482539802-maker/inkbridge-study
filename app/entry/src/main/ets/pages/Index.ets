import { common } from '@kit.AbilityKit';
import { captureController } from '../ink/CaptureController';
import { notesFiles } from '../ink/NotesFiles';
import { notificationCapture } from '../ink/NotificationCapture';

@Entry
@Component
struct Index {
  @State running: boolean = false;
  @State busy: boolean = false;
  @State authorizing: boolean = false;
  @State limitIndex: number = 0;
  @State message: string = '';
  @State syncMessage: string = '';
  @State counts: string = '';
  @State filePath: string = '';
  @State sessions: Array<string> = [];
  @State filesMessage: string = '';
  @State filesBusy: boolean = false;
  @State notificationMessage: string = '';
  @State notificationEnabled: boolean = false;
  private refreshTimer: number = -1;

  private refresh(): void {
    const controller = captureController;
    this.running = controller.running;
    this.busy = controller.stopping || controller.authorizing;
    this.authorizing = controller.authorizing;
    this.message = controller.message;
    this.syncMessage = controller.syncMessage;
    this.counts = `收到 ${controller.points} 个点 · 笔输入 ${controller.penPoints} 个 · 原生队列丢点 ${controller.dropped} 个`;
    this.filePath = controller.filePath;
    this.filesMessage = notesFiles.message;
    this.filesBusy = notesFiles.busy;
    this.notificationMessage = notificationCapture.message;
    this.notificationEnabled = notificationCapture.enabled;
    this.sessions = controller.sessions.slice(-6).reverse().map((session) =>
      `${session.session_id.slice(0, 8)} · ${session.state} · ${session.point_count} 点 · ${session.synced ? '电脑已保存' : '等待同步／记录中'}`);
  }

  aboutToAppear(): void {
    captureController.initialize(this.getUIContext().getHostContext() as common.UIAbilityContext);
    this.refresh();
    this.refreshTimer = setInterval(() => { this.refresh(); }, 250);
  }

  aboutToDisappear(): void {
    // Page visibility is not capture lifetime. The Ability owns cleanup.
    if (this.refreshTimer >= 0) clearInterval(this.refreshTimer);
    this.refreshTimer = -1;
  }

  build() {
    Scroll() {
      Column({ space: 18 }) {
        Text('InkBridge 笔迹采集').fontSize(30).fontWeight(FontWeight.Bold)
        Text('在本页开始并授权，等显示“正在记录”后再回华为笔记。原始笔迹在平板保存，电脑断线后自动回补。')
          .fontSize(18).fontColor('#44515A')
        Text(this.message).fontSize(18)
        Row({ space: 16 }) {
          Button(this.running ? '停止记录' : '授权并开始记录')
            .enabled(!this.busy)
            .backgroundColor(this.running ? '#B54545' : '#16394F')
            .onClick(() => {
              if (this.running) captureController.stopCapture();
              else captureController.start(this.getUIContext().getHostContext() as common.UIAbilityContext);
            })
          Button('取消授权').enabled(this.authorizing)
            .onClick(() => { captureController.stopCapture('authorization_cancelled'); })
        }
        Row({ space: 16 }) {
          Text('记录时限')
          Select([{ value: '手动停止' }, { value: '30 分钟' }, { value: '2 小时' }])
            .selected(this.limitIndex).value(['手动停止', '30 分钟', '2 小时'][this.limitIndex]).enabled(!this.running && !this.busy)
            .onSelect((index: number) => { this.limitIndex = index; captureController.limitMs = [0, 1800000, 7200000][index]; })
        }
        Button(this.notificationEnabled ? '关闭通知栏采集控制' : '开启通知栏采集控制').onClick(() => {
          if (notificationCapture.enabled) notificationCapture.disable();
          else notificationCapture.enable(this.getUIContext().getHostContext() as common.UIAbilityContext);
        })
        Text(this.notificationMessage).fontSize(14).fontColor('#44515A')
        Text(this.counts).fontSize(17)
        Text(this.syncMessage).fontSize(16).fontColor('#44515A')
        Row({ space: 16 }) {
          Button('立即重连').onClick(() => { captureController.retry(); })
          Button('重新核对全部会话').onClick(() => { captureController.resyncAll(); })
        }
        Text('最近会话').fontSize(19).fontWeight(FontWeight.Bold)
        ForEach(this.sessions, (session: string) => { Text(session).fontSize(14) })
        Text('笔记文件').fontSize(19).fontWeight(FontWeight.Bold)
        Button('打开独立书写试验').enabled(!this.running && !this.busy && !this.filesBusy)
          .onClick(() => { this.getUIContext().getRouter().pushUrl({ url: 'pages/PenLab' }); })
        Row({ space: 16 }) {
          Button('读取导出的笔记文件').enabled(!this.running && !this.busy && !this.filesBusy)
            .onClick(() => { notesFiles.receive(this.getUIContext().getHostContext() as common.UIAbilityContext); })
          Button('保存电脑准备的 PDF').enabled(!this.running && !this.busy && !this.filesBusy)
            .onClick(() => { notesFiles.savePrepared(this.getUIContext().getHostContext() as common.UIAbilityContext); })
        }
        Text(this.filesMessage).fontSize(14).copyOption(CopyOptions.LocalDevice)
        Text('系统屏幕授权由你确认。画面缓冲不保存、不上传，声音关闭；只记录坐标、压力和笔事件。停止后继续同步已有文件。')
          .fontSize(16).fontColor('#44515A')
        Text('系统中断会结束当前会话。重新打开应用后，已有记录会继续同步；开始新记录需再次确认系统授权。')
          .fontSize(14).fontColor('#65717B')
        Text(this.filePath).fontSize(13).fontColor('#65717B').copyOption(CopyOptions.LocalDevice)
      }.padding(32).width('100%').alignItems(HorizontalAlign.Start)
    }.width('100%').height('100%').backgroundColor('#F3F6F8')
  }
}
