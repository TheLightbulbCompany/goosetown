import { useEffect, useState } from "react";

interface Props {
  /** ms timestamp the agent registered. Used to compute elapsed time. */
  createdAt: number;
  /**
   * Seconds beyond which the overlay flips to a "stuck — try regenerating?"
   * state. PixelLab end-to-end commonly takes 30-90s; 120s is a generous
   * upper bound. Defaults to 120 unless overridden in tests.
   */
  stuckAfterSec?: number;
}

const DEFAULT_STUCK_AFTER_SEC = 120;

export function SpriteWaitOverlay({ createdAt, stuckAfterSec = DEFAULT_STUCK_AFTER_SEC }: Props) {
  const [elapsed, setElapsed] = useState(() =>
    Math.max(0, Math.floor((Date.now() - createdAt) / 1000)),
  );

  useEffect(() => {
    const id = setInterval(() => {
      setElapsed(Math.max(0, Math.floor((Date.now() - createdAt) / 1000)));
    }, 1_000);
    return () => clearInterval(id);
  }, [createdAt]);

  const isStuck = elapsed >= stuckAfterSec;

  if (isStuck) {
    return (
      <div className="absolute inset-0 flex items-center justify-center bg-black/60 rounded text-center p-3">
        <div>
          <div className="font-display text-xs tracking-widest uppercase text-yellow-300">
            Still drawing
          </div>
          <div className="mt-2 font-body text-xs text-clay-100">
            {elapsed}s elapsed — sprite generation is unusually slow.
          </div>
          <div className="mt-1 font-body text-[10px] text-clay-300">
            Refresh, or contact support if this persists.
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="absolute inset-0 flex items-center justify-center bg-black/50 rounded text-sm text-white">
      <div className="text-center">
        <div className="animate-pulse font-body text-xs">Drawing your agent…</div>
        <div className="font-body text-xs opacity-70 mt-1">{elapsed}s</div>
      </div>
    </div>
  );
}
