/**
 * ExchangeTicker — a live running log of economy events at the bank, replacing
 * the static cork-board. Driven by the economy_event stream (TownProvider);
 * the in-world cues are the spectacle, this is the text echo.
 */
import { useTownGame } from './TownProvider';
import type { EconomyTickerEntry } from '../lib/economyTicker';
import CrumbAmount from './CrumbAmount.tsx';

const SHOWN = 10;

export function formatTickerLine(e: EconomyTickerEntry): { text: string; amount: number | null } {
  const t = `"${e.title}"`;
  switch (e.type) {
    case 'posted':
      return { text: `${e.actorName} posted ${t} — up to`, amount: e.amount };
    case 'awarded':
      return { text: `${e.actorName} won ${t}`, amount: null };
    case 'payout':
      return { text: `${e.actorName} paid`, amount: e.amount };
    case 'failed':
      return { text: `${t} fell through`, amount: null };
    default:
      return { text: `${e.actorName} · ${t}`, amount: e.amount };
  }
}

export function ExchangeTicker() {
  const { economyEvents } = useTownGame();
  const events = (economyEvents ?? []).slice(0, SHOWN);
  return (
    <div className="mt-6">
      <p className="font-display text-sm text-clay-300 tracking-wider mb-2">AT THE BANK</p>
      {events.length === 0 ? (
        <p className="text-clay-500 text-xs font-body italic">Quiet at the bank…</p>
      ) : (
        <ul className="flex flex-col gap-1">
          {events.map((e) => {
            const { text, amount } = formatTickerLine(e);
            return (
              <li key={e.id} className="font-body text-xs text-clay-300 break-words">
                {text}
                {amount != null && <> <CrumbAmount n={amount} className="text-xs" /></>}
                {e.type === 'payout' && ' ✓'}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

export default ExchangeTicker;
