import { Link, useLocation } from 'react-router-dom';
import { useUser } from '@clerk/clerk-react';
import clsx from 'clsx';
import { SCENES } from '../scenes';

export default function TownNav() {
  const location = useLocation();
  const { user, isSignedIn } = useUser();

  const isActive = (path: string) => {
    if (path === '/') return location.pathname === '/';
    return location.pathname === path;
  };

  const visibleScenes = SCENES.filter(
    s => s.showInNav === 'always' || (s.showInNav === 'signed_in' && isSignedIn),
  );

  return (
    <nav className="flex items-center gap-4 px-4 py-2 bg-clay-900 border-b border-clay-700 z-20 shrink-0">
      <span className="font-display text-xl tracking-wider text-brown-200 game-title mr-4">
        GooseTown
      </span>

      {visibleScenes.map(s => (
        <Link
          key={s.id}
          to={s.route}
          className={clsx(
            'font-body text-sm px-3 py-1 rounded transition-colors',
            isActive(s.route)
              ? 'bg-clay-700 text-brown-100'
              : 'text-clay-300 hover:text-brown-100 hover:bg-clay-700/50',
          )}
        >
          {s.displayName}
        </Link>
      ))}

      {user && (
        <span className="ml-auto font-body text-xs text-clay-300">
          {user.firstName || user.username || 'Player'}
        </span>
      )}
    </nav>
  );
}
