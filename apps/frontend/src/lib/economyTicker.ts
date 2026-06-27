export interface EconomyTickerEntry {
  id: string;
  type: string;
  actorName: string;
  counterpartyName: string | null;
  amount: number | null;
  title: string;
}

const CAP = 20;

export function toTickerEntry(msg: Record<string, unknown>, id: string): EconomyTickerEntry {
  return {
    id,
    type: String(msg.type ?? ''),
    actorName: String(msg.actorName ?? ''),
    counterpartyName: (msg.counterpartyName as string | null) ?? null,
    amount: (msg.amount as number | null) ?? null,
    title: String(msg.title ?? ''),
  };
}

export function capEvents(prev: EconomyTickerEntry[], next: EconomyTickerEntry): EconomyTickerEntry[] {
  return [next, ...prev].slice(0, CAP);
}
