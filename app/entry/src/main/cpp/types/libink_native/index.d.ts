export interface InkPoint {
  type: string;
  seq: number;
  action: number;
  pointer_id: number;
  x: number;
  y: number;
  action_time: number;
  down_time: number;
  pressure: number;
  tool_type: number;
  window_id: number;
  display_id: number;
}
export interface CaptureStats {
  running: boolean;
  point_count: number;
  dropped_count: number;
}
export interface ClockSample {
  monotonic_before_us: number;
  monotonic_after_us: number;
  boottime_us: number;
  unix_ms: number;
}
declare const inkNative: {
  start(listener: (point: InkPoint) => void): number;
  stop(): Promise<number>;
  status(): CaptureStats;
  clockSample(): ClockSample;
  startClock(directory: string, sessionId: string): boolean;
  stopClock(): void;
  beginScreen(listener: (state: number) => void, geometryDirectory?: string): number;
  endScreen(): void;
};
export default inkNative;
