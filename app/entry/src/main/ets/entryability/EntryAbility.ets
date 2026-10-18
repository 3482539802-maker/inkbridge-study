import { UIAbility } from '@kit.AbilityKit';
import { window } from '@kit.ArkUI';
import { captureController } from '../ink/CaptureController';
import { PenContext } from '../ink/PenContext';
import { notificationCapture } from '../ink/NotificationCapture';

export default class EntryAbility extends UIAbility {
  onForeground(): void {
    captureController.setForeground(true);
  }
  onBackground(): void {
    captureController.setForeground(false);
  }
  onWindowStageCreate(stage: window.WindowStage): void {
    PenContext.set(this.context);
    stage.loadContent('pages/Index');
  }
  onDestroy(): void {
    notificationCapture.disable();
    captureController.shutdown();
  }
}
