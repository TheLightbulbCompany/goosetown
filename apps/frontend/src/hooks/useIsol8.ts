import { useCallback, useContext, useEffect, useState } from 'react';
import { Isol8Context } from '../components/Isol8Provider';

/**
 * Subscribe to real-time state updates via WebSocket.
 * Replaces Convex's useQuery for live world state.
 */
export function useTownState<T = any>(key: string = 'state_update'): T | undefined {
  const client = useContext(Isol8Context);
  const [state, setState] = useState<T | undefined>(
    () => client?.getLatest(key) as T | undefined,
  );

  useEffect(() => {
    if (!client) return;
    const unsubscribe = client.subscribe(key, (data) => {
      setState(data as T);
    });
    return unsubscribe;
  }, [client, key]);

  return state;
}

/**
 * Send a mutation (REST POST) to the backend.
 * Replaces Convex's useMutation.
 */
export function useTownMutation(endpoint: string) {
  const client = useContext(Isol8Context);

  return useCallback(
    async (args: any) => {
      if (!client) throw new Error('Isol8 client not available');
      return client.mutation(endpoint, args);
    },
    [client, endpoint],
  );
}

/**
 * One-shot query (REST GET). Not real-time.
 * Replaces Convex's useQuery for static data (descriptions, maps).
 */
export function useTownQuery<T = any>(endpoint: string, args?: any): T | undefined {
  const client = useContext(Isol8Context);
  const [data, setData] = useState<T | undefined>();

  useEffect(() => {
    if (!client) return;
    client.query(endpoint, args).then(setData).catch(console.error);
  }, [client, endpoint, JSON.stringify(args)]);

  return data;
}
