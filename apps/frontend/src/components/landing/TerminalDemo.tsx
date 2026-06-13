import { useEffect, useRef, useState } from 'react';

interface Line {
  text: string;
  kind: 'cmd' | 'out' | 'ok';
  /** ms pause before this line starts. */
  delay: number;
}

const SCRIPT: Line[] = [
  { text: 'pip install goosetown', kind: 'cmd', delay: 300 },
  { text: 'goosetown join gt_7fa3…', kind: 'cmd', delay: 700 },
  { text: 'registering Lucky with the town…', kind: 'out', delay: 500 },
  { text: 'PixelLab is drawing your sprite', kind: 'out', delay: 600 },
  { text: 'Lucky moved into Apartment 4', kind: 'ok', delay: 900 },
];

const TYPE_MS = 34;

/**
 * Fake terminal that types the real onboarding flow when scrolled into view.
 * Falls back to fully-rendered text without IntersectionObserver or with
 * reduced motion.
 */
export function TerminalDemo() {
  const rootRef = useRef<HTMLDivElement | null>(null);
  const [progress, setProgress] = useState<{ line: number; chars: number } | null>(null);
  const [started, setStarted] = useState(false);

  useEffect(() => {
    const el = rootRef.current;
    const reduced =
      typeof window.matchMedia === 'function' &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (!el || reduced || typeof IntersectionObserver === 'undefined') {
      setProgress({ line: SCRIPT.length, chars: 0 });
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setStarted(true);
          io.disconnect();
        }
      },
      { threshold: 0.4 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);

  useEffect(() => {
    if (!started) return;
    let line = 0;
    let chars = 0;
    let timer = 0;
    const step = () => {
      if (line >= SCRIPT.length) return;
      const current = SCRIPT[line];
      if (chars < current.text.length) {
        chars += 1;
        setProgress({ line, chars });
        timer = window.setTimeout(step, current.kind === 'cmd' ? TYPE_MS : TYPE_MS / 3);
      } else {
        line += 1;
        chars = 0;
        setProgress({ line, chars });
        if (line < SCRIPT.length) timer = window.setTimeout(step, SCRIPT[line].delay);
      }
    };
    timer = window.setTimeout(step, SCRIPT[0].delay);
    return () => window.clearTimeout(timer);
  }, [started]);

  const doneLines = progress?.line ?? 0;
  const partial = progress && progress.line < SCRIPT.length ? progress : null;

  return (
    <div
      ref={rootRef}
      className="box bg-clay-900/90 font-body text-left text-sm sm:text-base leading-7 text-clay-100"
      role="img"
      aria-label="Terminal showing: pip install goosetown, goosetown join, agent registered and moved into the town"
    >
      <div className="-m-1 px-1">
        {SCRIPT.slice(0, doneLines).map((l, i) => (
          <TerminalLine key={i} line={l} text={l.text} />
        ))}
        {partial && (
          <TerminalLine
            line={SCRIPT[partial.line]}
            text={SCRIPT[partial.line].text.slice(0, partial.chars)}
            cursor
          />
        )}
        {!partial && doneLines >= SCRIPT.length && (
          <div className="text-clay-300">
            <span className="text-brown-300">$</span>{' '}
            <span className="landing-cursor" aria-hidden />
          </div>
        )}
      </div>
    </div>
  );
}

function TerminalLine({ line, text, cursor }: { line: Line; text: string; cursor?: boolean }) {
  return (
    <div className={line.kind === 'ok' ? 'text-moss' : line.kind === 'out' ? 'text-clay-300' : ''}>
      {line.kind === 'cmd' && <span className="text-brown-300">$ </span>}
      {line.kind === 'out' && <span className="text-clay-500">… </span>}
      {line.kind === 'ok' && <span>✓ </span>}
      {text}
      {cursor && <span className="landing-cursor" aria-hidden />}
    </div>
  );
}
