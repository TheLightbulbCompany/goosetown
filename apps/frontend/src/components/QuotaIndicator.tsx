import { useQuota } from '../hooks/useQuota';

export function QuotaIndicator() {
  const data = useQuota();
  if (!data) return null;

  return (
    <div className="mt-3 border-t border-clay-700 pt-3 px-3 pb-3 space-y-2 text-xs font-body">
      <div className="font-display tracking-[0.2em] text-clay-300 uppercase text-[10px]">
        Your quotas
      </div>
      <QuotaBar
        label="agents"
        used={data.agents.used}
        limit={data.agents.limit}
      />
      <QuotaBar
        label="sprite-gens (today)"
        used={data.sprite_gen.used_24h}
        limit={data.sprite_gen.limit}
      />
    </div>
  );
}

function QuotaBar({ label, used, limit }: { label: string; used: number; limit: number }) {
  const ratio = Math.min(1, used / Math.max(1, limit));
  const atLimit = used >= limit;
  const nearLimit = !atLimit && ratio >= 0.8;
  const barColor = atLimit
    ? 'bg-red-500/80'
    : nearLimit
      ? 'bg-yellow-400/80'
      : 'bg-brown-500/80';
  const labelColor = atLimit ? 'text-red-300' : 'text-clay-100';

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <span className={labelColor}>{label}</span>
        <span className={`tabular-nums ${labelColor}`}>
          {used} / {limit}
        </span>
      </div>
      <div className="mt-1 h-1.5 w-full rounded-full bg-clay-900 overflow-hidden border border-clay-700">
        <div
          className={`h-full ${barColor} transition-[width] duration-300`}
          style={{ width: `${Math.max(2, ratio * 100)}%` }}
          aria-hidden
        />
      </div>
    </div>
  );
}
