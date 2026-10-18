import { common } from '@kit.AbilityKit';
import { captureController } from '../ink/CaptureController';
import { notificationCapture } from '../ink/NotificationCapture';

@Entry
@Component
struct Index {
  @State running: boolean = false;
  @State busy: boolean = false;
  @State authorizing: boolean = false;
  @State stopping: boolean = false;
  @State limitIndex: number = 0;
  @State message: string = '';
  @State syncTitle: string = '';
  @State syncDetail: string = '';
  @State pending: number = 0;
  @State elapsed: string = '00:00';
  @State penPoints: number = 0;
  @State dropped: number = 0;
  @State sessions: Array<string> = [];
  @State settingsOpen: boolean = false;
  @State historyOpen: boolean = false;
  @State notificationMessage: string = '';
  @State notificationEnabled: boolean = false;
  private refreshTimer: number = -1;

  private refresh(): void {
    const controller = captureController;
    this.running = controller.running;
    this.stopping = controller.stopping;
    this.busy = controller.stopping || controller.authorizing;
    this.authorizing = controller.authorizing;
    this.message = controller.message;
    this.penPoints = controller.penPoints;
    this.dropped = controller.dropped;
    this.limitIndex = controller.limitMs === 1800000 ? 1 : controller.limitMs === 7200000 ? 2 : 0;
    this.notificationMessage = notificationCapture.message;
    this.notificationEnabled = notificationCapture.enabled;

    const completed = controller.sessions.filter((session) => session.state !== 'recording');
    this.pending = completed.filter((session) => !session.synced).length;
    if (this.pending > 0) {
      this.syncTitle = `${this.pending} 段记录等待电脑确认`;
      this.syncDetail = '连接恢复后会自动补传，请保留平板上的记录。';
    } else if (this.running) {
      this.syncTitle = controller.connected ? '电脑已连接' : '等待连接电脑';
      this.syncDetail = '笔迹先保存在平板，断线也会继续记录。';
    } else if (completed.length > 0) {
      this.syncTitle = '已有记录均获电脑保存确认';
      this.syncDetail = '开始下一次记录时，需要重新确认系统授权。';
    } else {
      this.syncTitle = '记录优先保存在平板';
      this.syncDetail = '电脑连接后自动同步，无需手动传输。';
    }

    const active = controller.sessions.find((session) => session.state === 'recording');
    const seconds = active ? Math.max(0, Math.floor((Date.now() - active.started_at) / 1000)) : 0;
    this.elapsed = `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}`;
    this.sessions = completed.slice(-5).reverse().map((session) => {
      const date = new Date(session.started_at);
      const time = `${date.getMonth() + 1}/${date.getDate()} ${date.getHours().toString().padStart(2, '0')}:${date.getMinutes().toString().padStart(2, '0')}`;
      const state = session.state === 'interrupted' ? '记录中断' : '已结束';
      return `${time} · ${state} · ${session.synced ? '电脑已保存' : '等待同步'}`;
    });
  }

  private guidance(): string {
    if (this.stopping) return '正在结束记录并保存，请稍候。';
    if (this.authorizing) return this.message;
    if (this.running) return '可以回到华为笔记书写，写完后回来停止记录。';
    if (this.message.startsWith('记录已结束：time_limit')) return '已到设定时限，记录已结束；电脑同步会继续。';
    if (this.message.startsWith('记录已结束：authorization_cancelled')) return '已取消授权，尚未开始记录。';
    if (this.message.startsWith('记录已结束：authorization_timeout')) return '授权等待超时。请留在本页，重新开始并确认系统授权。';
    if (this.message.startsWith('记录已结束：local_write_failed')) return '本地保存出现异常，记录已停止。请检查平板存储空间；已有原始文件保留，请先核查完整性。';
    if (this.message.startsWith('记录已结束：storage_limit')) return '本次记录已到存储上限，请先等待同步，再开始新的记录。';
    if (this.message.startsWith('记录已结束：')) return '记录已被系统中断。已有原始文件保留；重新开始需要再次授权。';
    if (this.message.startsWith('在本页开始')) return '开始并确认系统授权，看到“正在记录”后再回华为笔记。';
    return this.message;
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
      Column({ space: 24 }) {
        Column({ space: 6 }) {
          Text('InkBridge').fontSize(18).fontColor('#52716C').fontWeight(FontWeight.Medium)
          Text('笔迹记录').fontSize(32).fontWeight(FontWeight.Bold).fontColor('#19352F')
        }.width('100%').alignItems(HorizontalAlign.Start)

        Column({ space: 22 }) {
          Row({ space: 10 }) {
            Circle().width(10).height(10).fill(this.running ? '#218365' : '#84938F')
            Text(this.stopping ? '正在保存' : this.authorizing ? '等待授权' : this.running ? '正在记录' : '未在记录')
              .fontSize(24).fontWeight(FontWeight.Bold).fontColor('#19352F')
          }.width('100%')
          if (this.running && !this.stopping) {
            Column({ space: 6 }) {
              Text(this.elapsed).fontSize(48).fontWeight(FontWeight.Medium).fontColor('#19352F')
              Text(`已接收 ${this.penPoints} 个笔点`).fontSize(15).fontColor('#5E706A')
            }.width('100%').alignItems(HorizontalAlign.Start)
          }
          Text(this.guidance()).fontSize(17).lineHeight(27).fontColor('#50645C').width('100%')
          if (this.dropped > 0) {
            Text(`检测到 ${this.dropped} 个输入事件丢失，本次记录可能不完整。`)
              .fontSize(16).fontColor('#A23535').width('100%')
          }
          Button(this.stopping ? '正在保存…' : this.authorizing ? '等待系统授权…' : this.running ? '停止记录' : '授权并开始记录')
            .width('100%').height(58).fontSize(19).fontWeight(FontWeight.Medium)
            .enabled(!this.busy).backgroundColor(this.running ? '#A94440' : '#236951')
            .onClick(() => {
              if (this.running) captureController.stopCapture();
              else captureController.start(this.getUIContext().getHostContext() as common.UIAbilityContext);
              this.refresh();
            })
          if (this.authorizing) {
            Button('取消授权').width('100%').height(44).backgroundColor('#EEF3F0').fontColor('#365D4E')
              .onClick(() => { captureController.stopCapture('authorization_cancelled'); this.refresh(); })
          }
        }.padding(28).width('100%').backgroundColor('#FFFFFF').borderRadius(24)
          .alignItems(HorizontalAlign.Start)

        Column({ space: 8 }) {
          Text(this.syncTitle).fontSize(18).fontWeight(FontWeight.Medium).fontColor('#294D3E')
          Text(this.syncDetail).fontSize(15).lineHeight(24).fontColor('#63756D')
          if (this.pending > 0) {
            Button('重试同步').backgroundColor('#E5EDE7').fontColor('#365D4E')
              .onClick(() => { captureController.retry(); })
          }
        }.padding({ left: 4, right: 4 }).width('100%').alignItems(HorizontalAlign.Start)

        Column({ space: 16 }) {
          Button(this.settingsOpen ? '记录设置  −' : '记录设置  ＋')
            .width('100%').height(48).backgroundColor('#E9EFEB').fontColor('#365D4E')
            .onClick(() => { this.settingsOpen = !this.settingsOpen; })
          if (this.settingsOpen) {
            Column({ space: 16 }) {
              Text('记录时限').fontSize(16).fontColor('#294D3E')
              Select([{ value: '手动停止' }, { value: '30 分钟' }, { value: '2 小时' }])
                .selected(this.limitIndex).value(['手动停止', '30 分钟', '2 小时'][this.limitIndex])
                .enabled(!this.running && !this.busy)
                .onSelect((index: number) => {
                  this.limitIndex = index;
                  captureController.limitMs = [0, 1800000, 7200000][index];
                })
              Button(this.notificationEnabled ? '关闭通知栏快捷控制' : '开启通知栏快捷控制')
                .backgroundColor('#E5EDE7').fontColor('#365D4E')
                .onClick(() => {
                  if (notificationCapture.enabled) notificationCapture.disable();
                  else notificationCapture.enable(this.getUIContext().getHostContext() as common.UIAbilityContext);
                })
              Text(this.notificationMessage).fontSize(14).fontColor('#63756D')
              Text('系统屏幕授权由你确认，麦克风关闭。定位需要的画面仅临时处理，不保存、不上传；原始笔迹单独保存。')
                .fontSize(14).lineHeight(23).fontColor('#63756D')
              Text('诊断信息').fontSize(14).fontWeight(FontWeight.Medium).fontColor('#63756D')
              Text(this.message).fontSize(13).fontColor('#63756D').copyOption(CopyOptions.LocalDevice)
            }.padding(20).width('100%').alignItems(HorizontalAlign.Start)
              .backgroundColor('#FFFFFF').borderRadius(16)
          }
          if (this.sessions.length > 0) {
            Button(this.historyOpen ? '最近记录  −' : '最近记录  ＋')
              .width('100%').height(48).backgroundColor('#E9EFEB').fontColor('#365D4E')
              .onClick(() => { this.historyOpen = !this.historyOpen; })
            if (this.historyOpen) {
              Column({ space: 14 }) {
                ForEach(this.sessions, (session: string) => {
                  Text(session).fontSize(15).fontColor('#50645C').width('100%')
                }, (session: string, index: number) => `${index}-${session}`)
              }.padding(20).width('100%').alignItems(HorizontalAlign.Start)
                .backgroundColor('#FFFFFF').borderRadius(16)
            }
          }
        }.width('100%')
      }.padding({ left: 24, right: 24, top: 32, bottom: 32 })
        .width('100%').constraintSize({ maxWidth: 680 }).alignItems(HorizontalAlign.Start)
    }.width('100%').height('100%').backgroundColor('#F2F5F1')
  }
}
