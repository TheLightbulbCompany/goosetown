import { ReactNode, createContext, useEffect, useMemo } from 'react';

export interface Isol8ClientConfig {
  apiUrl: string;
  wsUrl: string;
  getToken: () => Promise<string | null>;
}

type Listener = (data: any) => void;

export class Isol8Client {
  private ws: WebSocket | null = null;
  private listeners: Map<string, Set<Listener>> = new Map();
  private latestState: Map<string, any> = new Map();
  private config: Isol8ClientConfig;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(config: Isol8ClientConfig) {
    this.config = config;
  }

  connect() {
    if (this.ws) return;
    this.ws = new WebSocket(this.config.wsUrl);

    this.ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      const key = data.type || 'state_update';
      this.latestState.set(key, data);
      const listeners = this.listeners.get(key);
      if (listeners) {
        listeners.forEach((fn) => fn(data));
      }
    };

    this.ws.onclose = () => {
      this.ws = null;
      this.reconnectTimer = setTimeout(() => this.connect(), 2000);
    };

    this.ws.onerror = () => {
      this.ws?.close();
    };
  }

  disconnect() {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.ws?.close();
    this.ws = null;
  }

  subscribe(key: string, listener: Listener): () => void {
    if (!this.listeners.has(key)) {
      this.listeners.set(key, new Set());
    }
    this.listeners.get(key)!.add(listener);

    const latest = this.latestState.get(key);
    if (latest) listener(latest);

    return () => {
      this.listeners.get(key)?.delete(listener);
    };
  }

  getLatest(key: string): any {
    return this.latestState.get(key);
  }

  async mutation(endpoint: string, args: any): Promise<any> {
    const token = await this.config.getToken();
    const response = await fetch(`${this.config.apiUrl}${endpoint}`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
      body: JSON.stringify(args),
    });
    if (!response.ok) {
      throw new Error(`Mutation failed: ${response.status} ${await response.text()}`);
    }
    return response.json();
  }

  async query(endpoint: string, args?: any): Promise<any> {
    const token = await this.config.getToken();
    const params = args ? `?${new URLSearchParams(args).toString()}` : '';
    const response = await fetch(`${this.config.apiUrl}${endpoint}${params}`, {
      headers: {
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
      },
    });
    if (!response.ok) {
      throw new Error(`Query failed: ${response.status}`);
    }
    return response.json();
  }
}

export const Isol8Context = createContext<Isol8Client | null>(null);

export default function Isol8Provider({
  config,
  children,
}: {
  config: Isol8ClientConfig;
  children: ReactNode;
}) {
  const client = useMemo(() => new Isol8Client(config), [config]);

  useEffect(() => {
    client.connect();
    return () => client.disconnect();
  }, [client]);

  return <Isol8Context.Provider value={client}>{children}</Isol8Context.Provider>;
}
