import { Link, useLocation } from 'react-router-dom';
import { SignedIn, useUser } from '@clerk/clerk-react';
import clsx from 'clsx';

export default function TownNav() {
  const location = useLocation();
  const { user } = useUser();

  const isActive = (path: string) => {
    if (path === '/') {
      return location.pathname === '/' || location.pathname === '/ai-town' || location.pathname === '/ai-town/';
    }
    return location.pathname === path || location.pathname === `/ai-town${path}`;
  };

  return (
    <nav className="flex items-center gap-4 px-4 py-2 bg-clay-900 border-b border-clay-700 z-20 shrink-0">
      <span className="font-display text-xl tracking-wider text-brown-200 game-title mr-4">
        GooseTown
      </span>

      <Link
        to="/"
        className={clsx(
          'font-body text-sm px-3 py-1 rounded transition-colors',
          isActive('/')
            ? 'bg-clay-700 text-brown-100'
            : 'text-clay-300 hover:text-brown-100 hover:bg-clay-700/50',
        )}
      >
        Town
      </Link>

      <SignedIn>
        <Link
          to="/apartment"
          className={clsx(
            'font-body text-sm px-3 py-1 rounded transition-colors',
            isActive('/apartment')
              ? 'bg-clay-700 text-brown-100'
              : 'text-clay-300 hover:text-brown-100 hover:bg-clay-700/50',
          )}
        >
          Apartment
        </Link>
      </SignedIn>

      {user && (
        <span className="ml-auto font-body text-xs text-clay-300">
          {user.firstName || user.username || 'Player'}
        </span>
      )}
    </nav>
  );
}
