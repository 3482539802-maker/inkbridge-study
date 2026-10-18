import { UIAbility } from '@kit.AbilityKit';
import { fileIo } from '@kit.CoreFileKit';
import { probeLegacyDataShare } from '../ink/legacy_share_probe';

// Explicit HDC-only diagnostic. No file picker, query/write, input capture or UI automation.
export default class NotesProbeAbility extends UIAbility {
  onCreate(): void {
    this.probe();
  }

  private async probe(): Promise<void> {
    try {
      const result = await probeLegacyDataShare(this.context);
      const file = fileIo.openSync(`${this.context.filesDir}/notes-backend-probe.json`,
        fileIo.OpenMode.CREATE | fileIo.OpenMode.WRITE_ONLY | fileIo.OpenMode.TRUNC);
      try { fileIo.writeSync(file.fd, result); fileIo.fsyncSync(file.fd); }
      finally { fileIo.closeSync(file); }
    } finally {
      await this.context.terminateSelf();
    }
  }
}
