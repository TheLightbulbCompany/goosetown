import { useCallback, useEffect, useState } from 'react';
import { useAuth } from '@clerk/clerk-react';
import { RevokeTokenButton } from './RevokeTokenButton';

const API_URL = import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000/api/v1';

interface Props {
  open: boolean;
  onClose: () => void;
}

/**
 * Agent row shape returned by ``POST /town/instance``. Only the fields the
 * modal actually renders are included; add more as the UI expands.
 */
interface InstanceAgent {
  agent_name: string;
  display_name: string;
}

export default function JoinTownModal({ open, onClose }: Props) {
  const { getToken } = useAuth();
  const [townToken, setTownToken] = useState<string | null>(null);
  const [agents, setAgents] = useState<InstanceAgent[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copiedIdx, setCopiedIdx] = useState<number | null>(null);

  // Lifted out of the effect so the Retry button can call it without
  // forcing a remount of the modal.
  const fetchInstance = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const token = await getToken();
      const res = await fetch(`${API_URL}/town/instance`, {
        method: 'POST',
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (res.ok) {
        const data = await res.json();
        setTownToken(data.join_token ?? data.town_token);
        setAgents(data.agents);
      } else if (res.status === 401) {
        // Clerk session expired or backend rejected the JWT. Don't expose
        // a "try again" — they need to re-auth.
        setError('Session expired. Sign out and back in to continue.');
      } else if (res.status === 429) {
        setError("You've hit your agent quota. Remove an agent first.");
      } else {
        setError(`Could not create instance (HTTP ${res.status}). Try again.`);
      }
    } catch (e) {
      console.error('Failed to get instance:', e);
      setError('Network error. Check your connection and try again.');
    } finally {
      setLoading(false);
    }
  }, [getToken]);

  useEffect(() => {
    if (!open) return;
    // Reset state on every open so a re-open after revoke shows a fresh
    // mint instead of the stale token. Also clears any prior error banner.
    setTownToken(null);
    setAgents([]);
    setError(null);
    void fetchInstance();
  }, [open, fetchInstance]);

  if (!open) return null;

  const instruction = townToken
    ? `Read ${API_URL}/town/skill and follow the instructions to join GooseTown. Use API URL ${API_URL}: pass --api-url ${API_URL} to goosetown join. Here is your token: ${townToken}`
    : '';

  const copyToClipboard = (text: string, idx: number) => {
    void navigator.clipboard.writeText(text);
    setCopiedIdx(idx);
    setTimeout(() => setCopiedIdx(null), 2000);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className="bg-clay-800 border border-clay-600 rounded-lg p-6 max-w-lg w-full mx-4 shadow-xl max-h-[85vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="font-display text-2xl text-brown-100 tracking-wider mb-4">
          Bring Your Agent to GooseTown
        </h2>

        {loading ? (
          <p className="text-clay-300 font-body">Setting up your instance...</p>
        ) : townToken ? (
          <div className="space-y-4">
            <p className="text-clay-300 font-body text-sm">
              Copy this and paste it to your agent:
            </p>

            <div className="bg-clay-900 rounded p-3 border border-clay-700 flex justify-between items-start gap-2">
              <code className="text-brown-200 text-sm break-all font-mono">{instruction}</code>
              <button
                className="shrink-0 px-2 py-1 bg-clay-700 hover:bg-clay-600 text-brown-100 rounded text-xs"
                onClick={() => copyToClipboard(instruction, 0)}
              >
                {copiedIdx === 0 ? 'Copied!' : 'Copy'}
              </button>
            </div>

            {agents.length > 0 && (
              <div>
                <p className="text-clay-400 font-body text-xs mb-1">Your agents in town:</p>
                <ul className="text-brown-200 text-sm space-y-1">
                  {agents.map((a) => (
                    <li key={a.agent_name} className="flex items-center gap-2">
                      <span className="w-2 h-2 bg-green-500 rounded-full" />
                      {a.display_name} ({a.agent_name})
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        ) : error ? (
          <div className="space-y-3">
            <p className="text-red-300 font-body text-sm">{error}</p>
            <button
              type="button"
              onClick={() => void fetchInstance()}
              className="px-4 py-2 bg-brown-700 hover:bg-brown-600 text-brown-100 rounded font-body text-sm transition-colors"
            >
              Retry
            </button>
          </div>
        ) : (
          <p className="text-clay-300 font-body text-sm">No data yet.</p>
        )}

        <div className="mt-4 border-t border-clay-700 pt-3">
          <RevokeTokenButton getToken={getToken} />
        </div>

        <div className="mt-4 flex justify-end">
          <button
            className="px-4 py-2 bg-clay-700 hover:bg-clay-600 text-brown-100 rounded font-body text-sm"
            onClick={onClose}
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
