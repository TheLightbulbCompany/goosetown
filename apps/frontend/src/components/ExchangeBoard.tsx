/**
 * ExchangeBoard — the town notice board, read-only.
 *
 * An owner glancing at the bank's cork board: open jobs are pinned paper
 * notices (parchment, pin dot, posted-by line), each OPEN notice carries a
 * "town clock" strip that drains as its sealed-bid window closes, and
 * settled notices get a diagonal ink stamp (PAID in brown ink, FAILED /
 * EXPIRED in wax-seal red). Crumbs never appear as bare numbers — always
 * via <CrumbAmount/>.
 */

import { useState } from 'react';
import { useExchangeBoard } from '../hooks/useExchangeBoard.ts';
import type { ExchangeJob } from '../hooks/useExchangeBoard.ts';
import CrumbAmount from './CrumbAmount.tsx';

const SETTLED_STATUSES = ['COMPLETED', 'FAILED', 'EXPIRED', 'CANCELLED'] as const;
const SETTLED_SHOWN = 6;

/** Parse epoch-seconds, epoch-ms, or ISO strings into ms. NaN when invalid. */
function toMs(value: string | number | undefined): number {
  if (value == null) return NaN;
  if (typeof value === 'number') {
    // Heuristic: epoch seconds are ~1.7e9, epoch ms ~1.7e12.
    return value < 1e12 ? value * 1000 : value;
  }
  const asNumber = Number(value);
  if (value !== '' && !Number.isNaN(asNumber)) {
    return asNumber < 1e12 ? asNumber * 1000 : asNumber;
  }
  return new Date(value).getTime();
}

/**
 * Town-clock math: fraction of the bid window still remaining, clamped to
 * [0, 1]. Returns null when either timestamp is missing/invalid or the
 * window is degenerate — callers hide the clock strip in that case.
 */
export function clockRemainingFraction(
  createdAt: string | number | undefined,
  endsAt: string | number | undefined,
  nowMs: number = Date.now(),
): number | null {
  const start = toMs(createdAt);
  const end = toMs(endsAt);
  if (Number.isNaN(start) || Number.isNaN(end) || end <= start) return null;
  return Math.max(0, Math.min(1, (end - nowMs) / (end - start)));
}

/** Diagonal ink stamp for a settled notice. */
export function stampFor(status: string): { label: string; tone: 'ink' | 'wax' } {
  switch (status) {
    case 'COMPLETED':
      return { label: 'PAID', tone: 'ink' };
    case 'FAILED':
      return { label: 'FAILED', tone: 'wax' };
    case 'EXPIRED':
      return { label: 'EXPIRED', tone: 'wax' };
    default:
      return { label: 'VOID', tone: 'ink' };
  }
}

function InkStamp({ status }: { status: string }) {
  const { label, tone } = stampFor(status);
  return (
    <span
      className={`font-display text-[10px] tracking-wider border px-1 -rotate-6 shrink-0 ${
        tone === 'wax' ? 'text-wax border-wax' : 'text-brown-700 border-brown-700'
      }`}
    >
      {label}
    </span>
  );
}

/** The 2×2px pin dot that holds a notice to the board. */
function PinDot() {
  return (
    <span
      className="absolute top-1 left-1/2 -translate-x-1/2 w-0.5 h-0.5 rounded-full bg-clay-700"
      aria-hidden="true"
    />
  );
}

function PinnedNotice({ job }: { job: ExchangeJob }) {
  const fraction = clockRemainingFraction(job.created_at, job.bid_window_ends_at);
  return (
    <div className="relative bg-brown-200 p-3 pt-2.5">
      <PinDot />
      <h4 className="font-display text-sm text-brown-900 tracking-wider break-words">
        {job.title}
      </h4>
      <p className="font-body text-[11px] text-brown-700 mt-1">
        posted by {job.consumer_name || 'someone in town'}
      </p>
      {job.description && (
        <p className="font-body text-xs text-brown-700 mt-1 break-words">
          {job.description.slice(0, 100)}
          {job.description.length > 100 ? '…' : ''}
        </p>
      )}
      <p className="font-body text-xs text-brown-800 mt-1.5">
        up to <CrumbAmount n={job.max_price} />
      </p>
      <p className="font-body text-[11px] text-brown-700 mt-0.5">
        {job.bid_count} sealed bid{job.bid_count === 1 ? '' : 's'}
      </p>
      {fraction !== null && (
        <div className="mt-2 h-[1.5px] bg-brown-300" data-testid="town-clock">
          <div
            className="h-full bg-brown-700"
            data-testid="town-clock-fill"
            style={{ width: `${fraction * 100}%` }}
          />
        </div>
      )}
    </div>
  );
}

function SettledRow({ job }: { job: ExchangeJob }) {
  return (
    <div className="bg-brown-200 px-3 py-1.5 flex items-center gap-2">
      <InkStamp status={job.status} />
      <span className="font-body text-xs text-brown-800 truncate flex-1">{job.title}</span>
      {job.agreed_price > 0 && (
        <CrumbAmount n={job.agreed_price} className="text-xs text-brown-800" />
      )}
    </div>
  );
}

export function ExchangeBoard() {
  const { jobs, loading, error } = useExchangeBoard();
  const [open, setOpen] = useState(true);

  const pinned = jobs.filter((j) => j.status === 'OPEN');
  const settled = jobs
    .filter((j) => (SETTLED_STATUSES as readonly string[]).includes(j.status))
    .sort((a, b) => toMs(b.created_at) - toMs(a.created_at))
    .slice(0, SETTLED_SHOWN);

  return (
    <div className="mt-6">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full text-left font-display text-sm text-clay-300 hover:text-clay-100 tracking-wider mb-2 flex items-center gap-1"
        aria-expanded={open}
      >
        <span aria-hidden="true">{open ? '▾' : '▸'}</span>
        EXCHANGE BOARD
      </button>
      {open && (
        <>
          {loading && jobs.length === 0 && (
            <p className="text-clay-500 text-xs font-body italic">Reading the board…</p>
          )}
          {error && !loading && (
            <p className="text-red-400 text-xs font-body">Board error: {error}</p>
          )}
          {!loading && !error && pinned.length === 0 && settled.length === 0 && (
            <div className="relative bg-brown-200 p-3 pt-2.5 -rotate-2">
              <PinDot />
              <p className="font-body text-xs text-brown-700 italic">
                Nothing pinned — the board is bare.
              </p>
            </div>
          )}
          {(pinned.length > 0 || settled.length > 0) && (
            <div className="flex flex-col gap-2 max-h-[40vh] overflow-y-auto pr-1">
              {pinned.length > 0 && (
                <>
                  <p className="font-body text-[10px] text-clay-500 tracking-wider">Pinned</p>
                  {pinned.map((j) => (
                    <PinnedNotice key={j.job_id} job={j} />
                  ))}
                </>
              )}
              {settled.length > 0 && (
                <>
                  <p className="font-body text-[10px] text-clay-500 tracking-wider mt-1">
                    Settled
                  </p>
                  {settled.map((j) => (
                    <SettledRow key={j.job_id} job={j} />
                  ))}
                </>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default ExchangeBoard;
