import clsx from 'clsx';
import { useState, useEffect } from 'react';
import type { ReactNode } from 'react';

interface GameLayoutProps {
  sidebar: ReactNode;
  children: ReactNode;
}

export default function GameLayout({ sidebar, children }: GameLayoutProps) {
  // Mobile only: sidebar renders as a slide-up bottom sheet. On md+ the
  // drawerOpen state is inert — md: classes pin the sidebar to its
  // original static-column layout regardless.
  const [drawerOpen, setDrawerOpen] = useState(false);

  // Close on Escape — standard sheet/modal affordance for keyboard users.
  useEffect(() => {
    if (!drawerOpen) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setDrawerOpen(false);
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [drawerOpen]);

  return (
    <div className="flex w-full h-full">
      {drawerOpen && (
        <div
          className="md:hidden fixed inset-0 z-30 bg-black/30"
          onClick={() => setDrawerOpen(false)}
          aria-hidden
          data-testid="drawer-backdrop"
        />
      )}
      {/* Agents sidebar — static column on md+, bottom sheet on mobile */}
      <div
        id="agents-sidebar"
        className={clsx(
          'flex-col overflow-y-auto px-4 py-4 bg-clay-900 text-brown-100 border-clay-700',
          drawerOpen
            ? 'flex fixed inset-x-0 bottom-0 top-[35%] z-40 rounded-t-xl border-t shadow-2xl md:static md:inset-auto md:z-auto md:w-80 md:shrink-0 md:rounded-none md:border-t-0 md:border-r md:shadow-none'
            : 'hidden md:flex shrink-0 w-80 border-r',
        )}
      >
        <button
          aria-label="Close agents panel"
          hidden={!drawerOpen}
          onClick={() => setDrawerOpen(false)}
          className="md:hidden self-end -mt-1 mb-1 px-3 py-1 text-clay-300 text-2xl leading-none"
        >
          ×
        </button>
        {sidebar}
      </div>

      {/* Map area */}
      <div className="relative flex-1 overflow-hidden bg-brown-900">
        {children}
        {/* Mobile drawer toggle — floats over the canvas */}
        <button
          aria-expanded={drawerOpen}
          aria-controls="agents-sidebar"
          onClick={() => setDrawerOpen(o => !o)}
          className="md:hidden absolute bottom-4 left-1/2 -translate-x-1/2 z-30 px-5 py-2.5 bg-clay-800/95 border border-clay-600 rounded-full font-display text-sm tracking-wider text-brown-100 shadow-lg"
        >
          Agents
        </button>
      </div>
    </div>
  );
}
