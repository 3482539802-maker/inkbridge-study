import { common } from '@kit.AbilityKit';
import { HandwriteComponent, HandwriteController, PenType } from '@kit.Penkit';
import { PenLabJournal, LabPoint, LabHistory } from '../ink/PenLabJournal';
import { PenContext } from '../ink/PenContext';
import { PenLabStore } from '../ink/PenLabStore';

@Entry
@Component
struct PenLab {
  // Keep the HSP constructor inside a guarded lifecycle call. PenContext must
  // also be populated explicitly; a UIContext alone is insufficient.
  private controller?: HandwriteController;
  private store?: PenLabStore;
  private journal: PenLabJournal = new PenLabJournal();
  private snapshot: string = '';
  private refreshTimer: number = -1;
  private autosaveTimer: number = -1;
  private loadTimer: number = -1;
  private pendingLoad: string = '';
  private initialLoad: boolean = false;
  private dirty: boolean = false;
  private restoring: boolean = false;
  private lastInteraction: number = 0;
  private closing: boolean = false;
  private canvasScale: number = 1;
  private saveTask?: Promise<boolean>;
  private scrollY: number = 0;
  @State ready: boolean = false;
  @State controllerCreated: boolean = false;
  @State busy: boolean = false;
  @State saved: boolean = false;
  @State message: string = '正在准备画布';
  @State counts: string = '';
  @State savedNames: string[] = [];
  @State savedIndex: number = 0;
  @State loading: boolean = false;

  aboutToAppear(): void {
    try {
      const context = this.getUIContext().getHostContext() as common.UIAbilityContext;
      PenContext.set(context);
      this.journal.start(context.filesDir);
      this.store = new PenLabStore(context.filesDir);
      this.snapshot = this.store.latest();
      this.refreshSaved();
      this.initializeCanvas();
      this.refreshTimer = setInterval(() => {
        this.counts = `已记录 ${this.journal.events} 次操作`;
        if (this.journal.error) this.message = this.journal.error;
      }, 500);
      this.autosaveTimer = setInterval(() => {
        if (this.dirty && Date.now() - this.lastInteraction > 1000 && this.ready &&
          !this.busy && !this.restoring && !this.closing) this.save();
      }, 15000);
    } catch (error) { this.message = `试写记录无法开始：${error}`; }
  }

  private initializeCanvas(): void {
    if (this.controllerCreated || this.closing) return;
    this.message = '正在准备画布';
    this.journal.add({ kind: 'controller_initializing' });
    try {
      this.getUIContext().runScopedTask(() => {
        this.controller = new HandwriteController();
      });
      this.controllerCreated = this.controller !== undefined;
      this.journal.add({ kind: 'controller_created' });
    } catch (error) {
      this.message = '画布初始化失败，可重试或返回';
      this.journal.add({ kind: 'controller_failed', detail: `${error}` });
    }
  }

  aboutToDisappear(): void {
    if (this.refreshTimer >= 0) clearInterval(this.refreshTimer);
    this.refreshTimer = -1;
    if (this.autosaveTimer >= 0) clearInterval(this.autosaveTimer);
    if (this.loadTimer >= 0) clearTimeout(this.loadTimer);
    if (!this.closing) this.finish(true);
  }

  private point(point: TouchObject): LabPoint {
    return { id: point.id, x: point.x, y: point.y,
      window_x: point.windowX, window_y: point.windowY, pressure: point.pressure };
  }

  private observe(event: TouchEvent): void {
    if (this.closing || this.restoring) return;
    // Includes tool/undo touches inside the official component. This is a
    // conservative save trigger, not a confirmed edit/undo event.
    this.dirty = true;
    this.lastInteraction = Date.now();
    const points: LabPoint[] = event.changedTouches.map((p: TouchObject) => this.point(p));
    const historical: LabHistory[] = event.getHistoricalPoints().map((p: HistoricalPoint): LabHistory => {
      return { event_timestamp: p.timestamp, force: p.force, point: this.point(p.touchObject) };
    });
    this.journal.add({ kind: 'touch', event_timestamp: event.timestamp, action: event.type,
      source_tool: event.sourceTool, force: event.pressure, points: points, historical: historical,
      scale: this.canvasScale, scroll_y: this.scrollY });
    // Do not stop propagation, transform events or render a competing ink layer.
  }

  private save(): Promise<boolean> {
    if (this.saveTask) return this.saveTask;
    this.saveTask = this.performSave().finally(() => { this.saveTask = undefined; });
    return this.saveTask;
  }

  private async performSave(): Promise<boolean> {
    const controller = this.controller;
    const store = this.store;
    if (!this.ready || !controller || !store || this.restoring) return false;
    this.busy = true;
    this.dirty = false;
    try {
      const context = this.getUIContext().getHostContext() as common.UIAbilityContext;
      const path = `${context.filesDir}/penlab-${this.journal.sessionId}-${Date.now()}.bin`;
      await controller.save(path);
      const size = store.verifySnapshot(path);
      store.remember(path);
      this.snapshot = path;
      this.saved = true;
      this.refreshSaved();
      this.journal.add({ kind: 'save', detail: `${path}; bytes=${size}` });
      await this.journal.sync();
      this.message = `已保存 ${new Date().toLocaleTimeString()}，下次进入会自动恢复`;
      return true;
    } catch (error) {
      this.dirty = true;
      this.journal.add({ kind: 'save_failed', detail: `${error}` });
      this.message = `保存未完成：${error}`; return false;
    }
    finally { this.busy = false; }
  }

  private refreshSaved(): void {
    if (!this.store) return;
    this.savedNames = this.store.snapshots();
    this.saved = this.savedNames.length > 0;
    if (this.snapshot) this.savedIndex = Math.max(0, this.savedNames.indexOf(this.store.name(this.snapshot)));
  }

  private restore(path: string): void {
    const controller = this.controller;
    if (!path || !controller) return;
    this.restoring = true;
    this.loading = true;
    this.pendingLoad = path;
    this.message = '正在恢复保存的内容';
    try {
      this.store?.verifySnapshot(path);
      this.journal.add({ kind: 'load_requested', detail: path });
      this.loadTimer = setTimeout(() => {
        if (this.restoring) {
          this.restoring = false;
          this.loading = false;
          this.ready = false;
          this.message = '恢复超时，旧文件已保留，请返回后重新打开';
          this.journal.add({ kind: 'load_timeout', detail: path });
        }
      }, 10000);
      controller.load(path);
    } catch (error) {
      clearTimeout(this.loadTimer);
      this.restoring = false;
      this.loading = false;
      this.ready = false;
      this.message = `打开未完成，旧文件已保留：${error}`;
      this.journal.add({ kind: 'load_failed', detail: `${error}` });
    }
  }

  private async loadSaved(): Promise<void> {
    const store = this.store;
    if (!store || this.busy || this.restoring || !this.savedNames.length) return;
    const path = store.path(this.savedNames[this.savedIndex]);
    // Preserve unsaved work before replacing the visible canvas.
    if (this.dirty && !await this.save()) return;
    this.restore(path);
  }

  private canvasReady(): void {
    this.ready = false;
    this.loading = true;
    this.initialLoad = true;
    this.message = '正在准备书写内容';
    this.journal.add({ kind: 'canvas_ready' });
    try {
      // The tested HSP emits an initial blank-page load after onInit. Wait
      // for it before requesting a snapshot, so it cannot acknowledge restore.
      this.loadTimer = setTimeout(() => {
        if (!this.initialLoad) return;
        this.initialLoad = false;
        this.loading = false;
        this.message = '画布准备超时，请返回后重新打开';
        this.journal.add({ kind: 'initial_load_timeout' });
      }, 10000);
      this.controller?.onLoad((error, pageId: string) => {
        // The tested HSP reports { code: 0, message: 'load success' }.
        // A non-null error argument alone does not indicate failure.
        const failed = error !== undefined && error !== null && error.code !== 0;
        this.journal.add({ kind: failed ? 'load_failed' : 'load_complete',
          detail: JSON.stringify({ code: error?.code, message: error?.message, page_id: pageId }) });
        if (this.initialLoad) {
          this.initialLoad = false;
          clearTimeout(this.loadTimer);
          this.loading = false;
          if (failed) {
            this.message = '画布准备失败，请返回后重新打开';
            return;
          }
          this.ready = true;
          if (this.snapshot) this.restore(this.snapshot);
          else this.message = '可以开始试写';
          return;
        }
        if (!this.restoring) return;
        clearTimeout(this.loadTimer);
        this.restoring = false;
        this.loading = false;
        if (failed) {
          this.ready = false;
          this.message = '恢复失败，旧文件已保留，请返回后重新打开';
          return;
        }
        this.snapshot = this.pendingLoad;
        this.dirty = false;
        try { this.store?.remember(this.snapshot); this.refreshSaved(); }
        catch (failure) { this.journal.add({ kind: 'index_failed', detail: `${failure}` }); }
        this.message = '已恢复保存的内容，可以继续书写';
      });
    } catch (error) {
      clearTimeout(this.loadTimer);
      this.initialLoad = false;
      this.loading = false;
      this.ready = false;
      this.message = `画布加载未完成：${error}`;
    }
  }

  private async finish(forced: boolean = false): Promise<boolean> {
    if (this.closing) return false;
    this.closing = true;
    if (this.ready && (this.dirty || !this.snapshot) && !await this.save() && !forced) {
      this.closing = false;
      return false;
    }
    try { await this.journal.close(); }
    catch (error) { this.message = `记录收尾未完成：${error}`; return false; }
    return true;
  }

  build() {
    Column({ space: 8 }) {
      Row({ space: 12 }) {
        Text('书写试验').fontSize(24).fontWeight(FontWeight.Bold).layoutWeight(1)
        Button('保存试写').enabled(this.ready && !this.busy && !this.loading).onClick(() => { this.save(); })
        Button('保存并返回').enabled(!this.busy && !this.loading).onClick(async () => {
          if (await this.finish()) this.getUIContext().getRouter().back();
        })
      }.padding({ left: 20, right: 20, top: 12 })
      Text('独立试写页，不修改华为笔记。先比较跟手感、轻重笔与手掌防误触。')
        .fontSize(15).fontColor('#52626A')
      Text(`${this.message}  ${this.counts}`).fontSize(14).fontColor('#52626A')
      if (this.savedNames.length > 0) {
        Row({ space: 10 }) {
          Select(this.savedNames.map((name: string): SelectOption => {
            return { value: new Date(Number(name.slice(name.lastIndexOf('-') + 1, -4))).toLocaleString() };
          })).selected(this.savedIndex).enabled(!this.busy && !this.loading)
            .onSelect((index: number) => { this.savedIndex = index; })
          Button('打开这份记录').enabled(this.ready && !this.busy && !this.loading)
            .onClick(() => { this.loadSaved(); })
          Text('定时在停笔后自动保存，下次自动恢复').fontSize(13)
        }
      }
      Stack() {
        if (this.controllerCreated && this.controller) {
        HandwriteComponent({
          handwriteController: this.controller!,
          defaultPenType: PenType.BALLPOINT_PEN,
          defaultPenInfo: [{ penType: PenType.PEN, penWidth: 3 },
            { penType: PenType.BALLPOINT_PEN, penWidth: 3 },
            { penType: PenType.PENCIL, penWidth: 3 },
            { penType: PenType.MARKER, penWidth: 3 },
            { penType: PenType.HIGHLIGHTER_BRUSH, penWidth: 3 }],
          maxCanvasHeight: 4000,
          onInit: () => { this.canvasReady(); },
          onScale: (value: number) => {
            this.canvasScale = value;
            this.journal.add({ kind: 'scale', scale: value });
          },
          onDidScroll: (value: number) => {
            this.scrollY = value;
            this.journal.add({ kind: 'scroll', scroll_y: value });
          }
        }).width('100%').height('100%').enabled(this.ready && !this.loading)
        } else {
          Button('重试加载画布').onClick(() => { this.initializeCanvas(); })
        }
      }.width('100%').layoutWeight(1).backgroundColor(Color.White)
        .onTouch((event: TouchEvent) => { this.observe(event); })
    }.width('100%').height('100%').backgroundColor('#F3F6F8')
  }
}
