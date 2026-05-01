import { useState } from "react";

const API_URL = import.meta.env.VITE_BACKEND_URL ?? "http://localhost:8000/api/v1";

interface Props {
  getToken: () => Promise<string | null>;
  onRevoked?: () => void;
}

export function RevokeTokenButton({ getToken, onRevoked }: Props) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!confirming) {
    return (
      <button
        type="button"
        onClick={() => setConfirming(true)}
        className="text-sm text-clay-300 hover:text-red-400 underline font-body"
      >
        Revoke all tokens
      </button>
    );
  }

  return (
    <div
      role="alertdialog"
      aria-label="Confirm token revocation"
      className="space-y-2 rounded border border-clay-600 bg-clay-900 p-3"
    >
      <p className="text-sm text-clay-300 font-body">
        Revoke all tokens? All your agents will need to be re-registered with fresh
        tokens minted from the GooseTown UI.
      </p>
      {error && <p className="text-sm text-red-400 font-body">{error}</p>}
      <div className="flex gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            setError(null);
            try {
              const jwt = await getToken();
              const response = await fetch(`${API_URL}/town/instance/revoke`, {
                method: "POST",
                headers: jwt ? { Authorization: `Bearer ${jwt}` } : {},
              });
              if (!response.ok && response.status !== 204) {
                throw new Error(`Revoke failed: HTTP ${response.status}`);
              }
              setConfirming(false);
              onRevoked?.();
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
            } finally {
              setBusy(false);
            }
          }}
          className="rounded bg-red-700 hover:bg-red-600 px-3 py-1 text-sm text-brown-100 font-body disabled:opacity-50"
        >
          {busy ? "Revoking…" : "Confirm revoke"}
        </button>
        <button
          type="button"
          onClick={() => {
            setConfirming(false);
            setError(null);
          }}
          disabled={busy}
          className="rounded border border-clay-600 px-3 py-1 text-sm text-clay-300 font-body hover:bg-clay-700 disabled:opacity-50"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
