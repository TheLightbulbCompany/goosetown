import type { ReactNode } from 'react';

interface GameLayoutProps {
  sidebar: ReactNode;
  children: ReactNode;
}

export default function GameLayout({ sidebar, children }: GameLayoutProps) {
  return (
    <div className="flex w-full h-full">
      {/* Left sidebar — agent cards */}
      <div className="flex flex-col overflow-y-auto shrink-0 w-80 px-4 py-4 border-r border-clay-700 bg-clay-900 text-brown-100">
        {sidebar}
      </div>
      {/* Map area */}
      <div className="relative flex-1 overflow-hidden bg-brown-900">
        {children}
      </div>
    </div>
  );
}
