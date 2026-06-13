/**
 * CrumbAmount — the only way crumbs are ever rendered.
 *
 * Crumbs are never bare numbers: every amount gets the hand-pixeled crumb
 * glyph (7px, crumb-gold) so the currency reads as bread crumbs, not cash.
 * The number inherits color by default; pass ``className`` to tint it
 * (e.g. gold-300 for credits, clay-300 for debits in the passbook).
 */

/** 7×7 pixel-grid crumb glyph — a few crumbs off a loaf, in crumb-gold. */
function CrumbGlyph() {
  return (
    <svg
      width="7"
      height="7"
      viewBox="0 0 7 7"
      className="inline-block shrink-0 text-gold-500"
      style={{ shapeRendering: 'crispEdges' }}
      aria-hidden="true"
      data-testid="crumb-glyph"
    >
      {/* main crumb */}
      <rect x="1" y="2" width="3" height="3" fill="currentColor" />
      <rect x="2" y="1" width="2" height="1" fill="currentColor" />
      {/* stray crumbs */}
      <rect x="5" y="1" width="1" height="1" fill="currentColor" />
      <rect x="5" y="4" width="1" height="1" fill="currentColor" />
      <rect x="2" y="6" width="1" height="1" fill="currentColor" />
    </svg>
  );
}

export default function CrumbAmount({
  n,
  className = '',
  signed = false,
}: {
  n: number;
  /** Extra classes for the number (color tint etc.). */
  className?: string;
  /** Render an explicit +/− sign (passbook lines). */
  signed?: boolean;
}) {
  const text = signed ? `${n >= 0 ? '+' : '−'}${Math.abs(n)}` : `${n}`;
  return (
    <span className={`inline-flex items-baseline gap-1 font-body ${className}`.trim()}>
      <span>{text}</span>
      <CrumbGlyph />
      <span className="sr-only"> crumbs</span>
    </span>
  );
}
