import { describe, it, expect } from 'vitest';
import { toTickerEntry, capEvents } from './economyTicker';

describe('toTickerEntry', () => {
  it('maps a payout economy_event into a ticker entry', () => {
    const msg = {
      event: 'economy_event', type: 'payout',
      actorId: 'p1', actorName: 'Pat',
      counterpartyId: 'c1', counterpartyName: 'Carol',
      amount: 18, title: 'Write a haiku',
    };
    const e = toTickerEntry(msg, 'id-1');
    expect(e).toEqual({
      id: 'id-1', type: 'payout', actorName: 'Pat',
      counterpartyName: 'Carol', amount: 18, title: 'Write a haiku',
    });
  });
});

describe('capEvents', () => {
  it('prepends newest and caps the list length', () => {
    const base = Array.from({ length: 20 }, (_, i) => ({
      id: `${i}`, type: 'posted', actorName: 'a', counterpartyName: null, amount: null, title: 't',
    }));
    const next = capEvents(base, { id: 'new', type: 'awarded', actorName: 'z', counterpartyName: null, amount: null, title: 't' });
    expect(next.length).toBe(20);
    expect(next[0].id).toBe('new'); // newest first
    expect(next.some((e) => e.id === '19')).toBe(false); // oldest dropped
  });
});
