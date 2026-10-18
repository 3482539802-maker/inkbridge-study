import { fileIo as fs } from '@kit.CoreFileKit';
import { util } from '@kit.ArkTS';

export class PenLabStore {
  private filesDir: string;
  constructor(filesDir: string) { this.filesDir = filesDir; }

  snapshots(): string[] {
    return fs.listFileSync(this.filesDir)
      .filter((name: string) => /^penlab-[0-9a-f-]+-\d+\.bin$/.test(name))
      .filter((name: string) => fs.statSync(`${this.filesDir}/${name}`).size > 0)
      .sort((a: string, b: string) => this.timestamp(b) - this.timestamp(a));
  }

  private timestamp(name: string): number {
    return Number(name.slice(name.lastIndexOf('-') + 1, -4));
  }

  path(name: string): string {
    if (!/^penlab-[0-9a-f-]+-\d+\.bin$/.test(name)) throw new Error('保存文件名无效');
    return `${this.filesDir}/${name}`;
  }

  name(path: string): string {
    const name = path.slice(path.lastIndexOf('/') + 1);
    if (this.path(name) !== path) throw new Error('保存文件不在本应用目录');
    return name;
  }

  verifySnapshot(path: string): number {
    this.name(path);
    const file = fs.openSync(path, fs.OpenMode.READ_WRITE);
    try {
      const size = fs.statSync(file.fd).size;
      if (size < 12) throw new Error('保存文件为空或不完整');
      const magic = new ArrayBuffer(12);
      if (fs.readSync(file.fd, magic) !== 12 ||
        new util.TextDecoder().decodeToString(new Uint8Array(magic)) !== 'PENCILENGINE') {
        throw new Error('保存文件格式未通过检查');
      }
      fs.fsyncSync(file.fd);
      return size;
    } finally { fs.closeSync(file); }
  }

  remember(path: string): void {
    this.atomicWrite('penlab-latest.json', JSON.stringify({ name: this.name(path) }));
  }

  latest(): string {
    const names = this.snapshots();
    try {
      const item = JSON.parse(fs.readTextSync(`${this.filesDir}/penlab-latest.json`)) as LatestSnapshot;
      if (names.includes(item.name)) return this.path(item.name);
    } catch (_) {}
    return names.length > 0 ? this.path(names[0]) : '';
  }

  private atomicWrite(name: string, text: string): void {
    const temporary = `${this.filesDir}/${name}.${util.generateRandomUUID()}.tmp`;
    const file = fs.openSync(temporary, fs.OpenMode.CREATE | fs.OpenMode.READ_WRITE);
    try {
      const bytes = new util.TextEncoder().encodeInto(text);
      let offset = 0;
      while (offset < bytes.length) {
        const count = fs.writeSync(file.fd, bytes.buffer.slice(offset));
        if (count <= 0) throw new Error('保存索引未写完整');
        offset += count;
      }
      fs.fsyncSync(file.fd);
    } finally { fs.closeSync(file); }
    fs.renameSync(temporary, `${this.filesDir}/${name}`);
  }
}

interface LatestSnapshot { name: string; }
