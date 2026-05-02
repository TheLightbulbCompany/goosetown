import { useEffect, useState } from "react";

interface Props {
  createdAt: number; // ms timestamp
}

export function SpriteWaitOverlay({ createdAt }: Props) {
  const [elapsed, setElapsed] = useState(() =>
    Math.max(0, Math.floor((Date.now() - createdAt) / 1000)),
  );

  useEffect(() => {
    const id = setInterval(() => {
      setElapsed(Math.max(0, Math.floor((Date.now() - createdAt) / 1000)));
    }, 1_000);
    return () => clearInterval(id);
  }, [createdAt]);

  return (
    <div className="absolute inset-0 flex items-center justify-center bg-black/50 rounded text-sm text-white">
      <div className="text-center">
        <div className="animate-pulse font-body text-xs">Drawing your agent…</div>
        <div className="font-body text-xs opacity-70 mt-1">{elapsed}s</div>
      </div>
    </div>
  );
}
