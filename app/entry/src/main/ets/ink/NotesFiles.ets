import { common } from '@kit.AbilityKit';
import { fileIo, picker } from '@kit.CoreFileKit';
import { BusinessError } from '@kit.BasicServicesKit';

class NotesFiles {
  message: string = '';
  busy: boolean = false;

  private copy(source: string, destination: string): number {
    const input = fileIo.openSync(source, fileIo.OpenMode.READ_ONLY);
    try {
      const size = fileIo.statSync(input.fd).size;
      if (size > 512 * 1024 * 1024) throw new Error('文件超过 512 MiB，未复制');
      const output = fileIo.openSync(destination, fileIo.OpenMode.CREATE | fileIo.OpenMode.WRITE_ONLY | fileIo.OpenMode.TRUNC);
      try {
        const buffer = new ArrayBuffer(256 * 1024);
        let total = 0;
        let count = fileIo.readSync(input.fd, buffer);
        while (count > 0) {
          let written = 0;
          while (written < count) {
            const bytes = fileIo.writeSync(output.fd, buffer.slice(written, count));
            if (bytes <= 0) throw new Error('文件写入未完成');
            written += bytes;
          }
          total += count;
          if (total > 512 * 1024 * 1024) throw new Error('文件在复制期间超过限制');
          count = fileIo.readSync(input.fd, buffer);
        }
        fileIo.fsyncSync(output.fd);
        return total;
      } finally {
        fileIo.closeSync(output);
      }
    } finally {
      fileIo.closeSync(input);
    }
  }

  async receive(context: common.UIAbilityContext): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    let partial = '';
    try {
      this.message = '选择华为笔记导出的 PDF 或 hinote 文件';
      const choices = await new picker.DocumentViewPicker(context).select({ maxSelectNumber: 1,
        fileSuffixFilters: ['笔记文件|.pdf,.hinote'] });
      if (choices.length !== 1) { this.message = '未选择文件'; return; }
      const name = decodeURIComponent(choices[0].split('/').pop() || '');
      const suffix = name.toLowerCase().endsWith('.pdf') ? '.pdf' :
        name.toLowerCase().endsWith('.hinote') ? '.hinote' : '';
      if (!suffix) throw new Error('仅接收 PDF／hinote');
      const path = `${context.filesDir}/notes-import-${Date.now()}${suffix}`;
      partial = path + '.partial';
      const bytes = this.copy(choices[0], partial);
      fileIo.renameSync(partial, path);
      partial = '';
      const metadata = JSON.stringify({ source_uri: choices[0], original_name: name,
        local_path: path, bytes: bytes, copied_at: new Date().toISOString() });
      const meta = fileIo.openSync(path + '.json', fileIo.OpenMode.CREATE | fileIo.OpenMode.WRITE_ONLY | fileIo.OpenMode.TRUNC);
      try { fileIo.writeSync(meta.fd, metadata); fileIo.fsyncSync(meta.fd); }
      finally { fileIo.closeSync(meta); }
      this.message = `已复制 ${bytes} 字节，可由电脑 HDC 读取：${path}`;
    } catch (error) {
      const failure = error as BusinessError;
      this.message = `读取文件失败 ${failure.code || ''}：${failure.message}`;
    } finally {
      if (partial) { try { fileIo.unlinkSync(partial); } catch (_) {} }
      this.busy = false;
    }
  }

  async savePrepared(context: common.UIAbilityContext): Promise<void> {
    if (this.busy) return;
    this.busy = true;
    try {
      // PC deliberately stages one PDF in this app's debug sandbox via HDC.
      // Save to a user-selected URI, then use Notes' normal Import PDF action.
      const source = `${context.filesDir}/notes-outgoing.pdf`;
      const input = fileIo.openSync(source, fileIo.OpenMode.READ_ONLY);
      try {
        const magic = new ArrayBuffer(5);
        if (fileIo.readSync(input.fd, magic) !== 5) throw new Error('电脑尚未准备 PDF');
        const bytes = new Uint8Array(magic);
        if (bytes[0] !== 37 || bytes[1] !== 80 || bytes[2] !== 68 || bytes[3] !== 70 || bytes[4] !== 45) {
          throw new Error('电脑准备的文件不是 PDF');
        }
      } finally { fileIo.closeSync(input); }
      this.message = '选择电脑 PDF 的保存位置；随后可在华为笔记中导入';
      const choices = await new picker.DocumentViewPicker(context).save({
        newFileNames: [`InkBridge-${Date.now()}.pdf`], fileSuffixChoices: ['PDF|.pdf'] });
      if (choices.length !== 1) { this.message = '未选择保存位置'; return; }
      const bytes = this.copy(source, choices[0]);
      this.message = `已保存 ${bytes} 字节。请在华为笔记 → 新建 → 导入 PDF 中选择此文件。`;
    } catch (error) {
      const failure = error as BusinessError;
      this.message = `保存失败 ${failure.code || ''}：${failure.message}`;
    } finally { this.busy = false; }
  }
}

export const notesFiles = new NotesFiles();
