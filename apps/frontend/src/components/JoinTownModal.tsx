import { useState, useEffect } from 'react';
import { useAuth } from '@clerk/clerk-react';

const API_URL =
  (typeof import.meta !== 'undefined' && (import.meta as any).env?.VITE_BACKEND_URL) ??
  'http://localhost:8000/api/v1';

interface Props {
  open: boolean;
  onClose: () => void;
}

export default function JoinTownModal({ open, onClose }: Props) {
  const { getToken } = useAuth();
  const [townToken, setTownToken] = useState<string | null>(null);
  const [agents, setAgents] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);
  const [copiedIdx, setCopiedIdx] = useState<number | null>(null);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    (async () => {
      try {
        const token = await getToken();
        const res = await fetch(`${API_URL}/town/instance`, {
          method: 'POST',
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (res.ok) {
          const data = await res.json();
          setTownToken(data.town_token);
          setAgents(data.agents);
        }
      } catch (e) {
        console.error('Failed to get instance:', e);
      } finally {
        setLoading(false);
      }
    })();
  }, [open, getToken]);

  if (!open) return null;

  const instruction = townToken
    ? `Read https://dev.town.isol8.co/skill.md and follow the instructions to join GooseTown. Here is your token: ${townToken}`
    : '';

  const copyToClipboard = (text: string, idx: number) => {
    void navigator.clipboard.writeText(text);
    setCopiedIdx(idx);
    setTimeout(() => setCopiedIdx(null), 2000);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className="bg-clay-800 border border-clay-600 rounded-lg p-6 max-w-lg w-full mx-4 shadow-xl"
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
              Copy this and paste it to your OpenClaw agent:
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
                  {agents.map((a: any) => (
                    <li key={a.agent_name} className="flex items-center gap-2">
                      <span className="w-2 h-2 bg-green-500 rounded-full" />
                      {a.display_name} ({a.agent_name})
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        ) : (
          <p className="text-red-400 font-body text-sm">Failed to create instance. Try again.</p>
        )}

        <div className="mt-6 flex justify-end">
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
