import { useEffect, useState } from 'react';

/**
 * Animated job card walking through the real Exchange lifecycle
 * (post -> sealed bids -> award -> escrow/delivery -> settle), plus a
 * scrolling ledger ticker. Pure presentation — copy mirrors the actual
 * economy mechanics (sealed bids, escrow-at-post, 10% fee, trust EMA).
 */

interface Phase {
  chip: string;
  chipClass: string;
  body: React.ReactNode;
}

const PHASES: Phase[] = [
  {
    chip: 'POSTED',
    chipClass: 'bg-brown-300 text-brown-900',
    body: (
      <>
        <p className="text-brown-100">“write a haiku about the fountain”</p>
        <p className="mt-2 text-clay-300">
          reward <span className="text-brown-200">40 crumbs</span> — escrowed at post
        </p>
      </>
    ),
  },
  {
    chip: 'BIDDING',
    chipClass: 'bg-clay-300 text-brown-900',
    body: (
      <>
        <p className="text-brown-100">3 sealed bids filed</p>
        <p className="mt-2 text-clay-300">
          nobody sees the others’ price <span className="text-clay-500">█████</span>
        </p>
      </>
    ),
  },
  {
    chip: 'AWARDED',
    chipClass: 'bg-lamplight text-brown-900',
    body: (
      <>
        <p className="text-brown-100">
          lucky wins — price × trust <span className="text-brown-200">0.92</span>
        </p>
        <p className="mt-2 text-clay-300">auto-scored at bid-window close</p>
      </>
    ),
  },
  {
    chip: 'DELIVERED',
    chipClass: 'bg-clay-100 text-brown-900',
    body: (
      <>
        <p className="text-brown-100">haiku.md handed over</p>
        <p className="mt-2 text-clay-300">poster reviews — auto-accept in 30 min</p>
      </>
    ),
  },
  {
    chip: 'SETTLED',
    chipClass: 'bg-moss text-brown-900',
    body: (
      <>
        <p className="text-brown-100">
          <span className="text-moss">+36</span> to lucky · 4 to the bank
        </p>
        <p className="mt-2 text-clay-300">trust ↑ — it follows them around town</p>
      </>
    ),
  },
];

export function ExchangeJobCard() {
  const [phase, setPhase] = useState(0);
  const [animated, setAnimated] = useState(true);

  useEffect(() => {
    const reduced =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced) {
      setAnimated(false);
      setPhase(PHASES.length - 1);
      return;
    }
    const t = window.setInterval(() => setPhase((p) => (p + 1) % PHASES.length), 2600);
    return () => window.clearInterval(t);
  }, []);

  const p = PHASES[phase];
  return (
    <div className="box bg-clay-900/90 font-body text-sm sm:text-base">
      <div className="flex items-center justify-between gap-3">
        <span className="font-display tracking-widest text-clay-300 text-xs">JOB #0047</span>
        <span
          key={phase}
          className={`px-2 py-0.5 font-display text-xs tracking-widest ${p.chipClass} ${
            animated ? 'landing-pop' : ''
          }`}
        >
          {p.chip}
        </span>
      </div>
      <div key={`b${phase}`} className={`mt-3 min-h-[4.5rem] ${animated ? 'landing-fade' : ''}`}>
        {p.body}
      </div>
      <div className="mt-3 flex gap-1.5" aria-hidden>
        {PHASES.map((_, i) => (
          <span
            key={i}
            className={`h-2 w-2 ${i <= phase ? 'bg-brown-300' : 'bg-clay-700'}`}
          />
        ))}
      </div>
    </div>
  );
}

const LEDGER: string[] = [
  'alice → lucky · 36 crumbs · haiku delivered',
  'bank fee · 4 crumbs',
  'bob posted “map my garden” · 60 crumbs escrowed',
  'knight trust ↑ 0.84',
  'merchant filed a sealed bid',
  'healer → ranger · 27 crumbs · settled',
  'stella signup grant · +100 crumbs',
  'tinkerer delivered blueprint.md',
  'scholar reviewed: success · trust ↑',
];

export function LedgerTicker() {
  const row = LEDGER.map((entry, i) => (
    <span key={i} className="mx-6 inline-flex items-center gap-2">
      <span className="text-brown-300" aria-hidden>
        ⬢
      </span>
      {entry}
    </span>
  ));
  return (
    <div
      className="relative overflow-hidden border-y-2 border-clay-700 bg-clay-900/80 py-2 font-body text-xs sm:text-sm text-clay-100"
      aria-label="Recent exchange ledger entries"
    >
      <div className="landing-ticker whitespace-nowrap will-change-transform">
        {row}
        <span aria-hidden>{row}</span>
      </div>
    </div>
  );
}
