import { useQuota } from '../hooks/useQuota';

export function QuotaIndicator() {
  const data = useQuota();
  if (!data) return null;

  const agentsAtLimit = data.agents.used >= data.agents.limit;
  const spriteAtLimit = data.sprite_gen.used_24h >= data.sprite_gen.limit;

  return (
    <div className="mt-2 space-y-1 px-3 py-2 text-xs border-t border-clay-700">
      <div className={agentsAtLimit ? 'text-red-500 font-medium' : 'text-clay-300'}>
        {data.agents.used} / {data.agents.limit} agents
      </div>
      <div className={spriteAtLimit ? 'text-red-500 font-medium' : 'text-clay-300'}>
        {data.sprite_gen.used_24h} / {data.sprite_gen.limit} sprite-gens (24h)
      </div>
    </div>
  );
}
