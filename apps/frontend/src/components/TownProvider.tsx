import { ReactNode, createContext, useContext, useCallback } from 'react';
import { ClerkProvider, useAuth } from '@clerk/clerk-react';
import { useTownState } from '../hooks/useTownState';
import type { TownGameState } from '../types/town';
import type { EconomyTickerEntry } from '../lib/economyTicker';

const CLERK_PUBLISHABLE_KEY = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY as string;

interface TownContextValue {
  game: TownGameState | undefined;
  economyEvents: EconomyTickerEntry[];
}

const TownContext = createContext<TownContextValue>({
  game: undefined,
  economyEvents: [],
});

export function useTownGame() {
  return useContext(TownContext);
}

function TownStateProvider({ children }: { children: ReactNode }) {
  const { getToken, userId } = useAuth();
  const getTokenFn = useCallback(() => getToken(), [getToken]);
  const { game, economyEvents } = useTownState(getTokenFn, userId);

  return (
    <TownContext.Provider value={{ game, economyEvents }}>
      {children}
    </TownContext.Provider>
  );
}

function UnauthTownStateProvider({ children }: { children: ReactNode }) {
  const { game, economyEvents } = useTownState();

  return (
    <TownContext.Provider value={{ game, economyEvents }}>
      {children}
    </TownContext.Provider>
  );
}

export default function TownProvider({ children }: { children: ReactNode }) {
  if (!CLERK_PUBLISHABLE_KEY) {
    return (
      <UnauthTownStateProvider>
        {children}
      </UnauthTownStateProvider>
    );
  }

  return (
    <ClerkProvider publishableKey={CLERK_PUBLISHABLE_KEY}>
      <TownStateProvider>
        {children}
      </TownStateProvider>
    </ClerkProvider>
  );
}
