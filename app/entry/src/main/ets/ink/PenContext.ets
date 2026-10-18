import { common } from '@kit.AbilityKit';

// Pen Kit's documented host-context contract. The system HSP reads this key
// before creating its resource manager; UIContext alone does not populate it.
declare namespace globalThis {
  let _brushEngineContext: common.UIAbilityContext;
}

export class PenContext {
  static set(context: common.UIAbilityContext): void {
    globalThis._brushEngineContext = context;
  }
}
