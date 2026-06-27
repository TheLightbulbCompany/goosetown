import { useEffect, useMemo, useState } from 'react';
import { Routes, Route, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { SignedIn, SignedOut, UserButton, useAuth } from '@clerk/clerk-react';
import { ToastContainer, toast } from 'react-toastify';
import TownNav from './components/TownNav.tsx';
import GameLayout from './components/GameLayout.tsx';
import GodotCanvas from './components/GodotCanvas.tsx';
import ApartmentCard from './components/ApartmentCard.tsx';
import ConversationsLog from './components/ConversationsLog.tsx';
import { WorldFeed } from './components/WorldFeed.tsx';
import { ExchangeTicker } from './components/ExchangeTicker.tsx';
import { ProvidersPanel } from './components/ProvidersPanel.tsx';
import LoginButton from './components/buttons/LoginButton.tsx';
import MusicButton from './components/buttons/MusicButton.tsx';
import JoinTownModal from './components/JoinTownModal.tsx';
import OwnerChatPanel from './components/OwnerChatPanel.tsx';
import { useApartment } from './hooks/useApartment.ts';
import { useQuota } from './hooks/useQuota.ts';
import { QuotaIndicator } from './components/QuotaIndicator.tsx';
import { OnboardingPanel } from './components/OnboardingPanel.tsx';
import { AnonymousViewerHint } from './components/AnonymousViewerHint.tsx';
import { SCENES, sceneForPath } from './scenes.ts';
import InteriorRoute from './components/InteriorRoute.tsx';
import SignInPage from './pages/SignIn.tsx';
import SignUpPage from './pages/SignUp.tsx';
import { Landing } from './pages/Landing.tsx';

const API_URL = import.meta.env.VITE_BACKEND_URL ?? 'http://localhost:8000/api/v1';

function AgentsSidebar({
  onJoinClick,
  onAgentChatClick,
}: {
  onJoinClick: () => void;
  onAgentChatClick: (agentId: string, agentName: string) => void;
}) {
  const { data, loading } = useApartment();
  const quota = useQuota();
  const { getToken } = useAuth();

  // Soft-delete: removes from town, frees a quota slot, preserves agent
  // record (TOWN_LIFE.md, sprite, conversations, relationships). The
  // useApartment hook polls every 2s so the UI will reflect the change
  // shortly without a manual refetch.
  const handleRemove = async (agentId: string, agentName: string) => {
    if (!confirm(
      `Remove ${agentName} from GooseTown?\n\n` +
      `They'll disappear from town immediately. Their diary, sprite, ` +
      `and history are preserved — you can reactivate them later.`,
    )) return;
    try {
      const token = await getToken();
      const res = await fetch(`${API_URL}/town/agent/${agentId}/deactivate`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) {
        const detail = await res.text();
        toast.error(`Couldn't remove ${agentName}: ${res.status} ${detail.slice(0, 80)}`);
        return;
      }
      toast.success(`${agentName} removed from town`);
    } catch (e) {
      toast.error(`Couldn't remove ${agentName}: ${e instanceof Error ? e.message : 'unknown error'}`);
    }
  };

  const handleReactivate = async (agentId: string, agentName: string) => {
    try {
      const token = await getToken();
      const res = await fetch(`${API_URL}/town/agent/${agentId}/reactivate`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.status === 429) {
        toast.error(`Can't reactivate ${agentName}: you're at the agent limit. Remove another agent first.`);
        return;
      }
      if (!res.ok) {
        const detail = await res.text();
        toast.error(`Couldn't reactivate ${agentName}: ${res.status} ${detail.slice(0, 80)}`);
        return;
      }
      toast.success(`${agentName} is back in town`);
    } catch (e) {
      toast.error(`Couldn't reactivate ${agentName}: ${e instanceof Error ? e.message : 'unknown error'}`);
    }
  };

  // Soft-gate Add/Join when at limit. Server-side enforcement still happens
  // in /town/agent/register; this just prevents the user from minting a
  // token they can't actually use.
  const atAgentLimit = quota ? quota.agents.used >= quota.agents.limit : false;
  const atSpriteLimit = quota ? quota.sprite_gen.used_24h >= quota.sprite_gen.limit : false;
  const joinDisabled = atAgentLimit || atSpriteLimit;
  const joinDisabledReason = atAgentLimit
    ? `You're at the ${quota?.agents.limit ?? 0}-agent limit. Remove one to add another.`
    : atSpriteLimit
      ? `You've used all ${quota?.sprite_gen.limit ?? 0} sprite generations in the last 24 hours.`
      : '';

  if (loading) {
    return <p className="font-body text-clay-300 text-sm p-2">Loading agents...</p>;
  }

  if (!data || data.agents.length === 0) {
    return (
      <>
        <SignedIn>
          <div className="p-2">
            <OnboardingPanel onJoinClick={onJoinClick} />
            <div className="space-y-3">
              <p className="font-body text-clay-300 text-sm">No agents in town yet.</p>
              <button
                onClick={onJoinClick}
                disabled={joinDisabled}
                title={joinDisabled ? joinDisabledReason : undefined}
                className="w-full px-4 py-2 bg-brown-600 hover:bg-brown-500 text-brown-100 rounded font-body text-sm transition-colors disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-brown-600"
              >
                Join GooseTown
              </button>
              {joinDisabled && (
                <p className="font-body text-xs text-yellow-300/80">{joinDisabledReason}</p>
              )}
            </div>
          </div>
        </SignedIn>
        <SignedOut>
          <AnonymousViewerHint />
        </SignedOut>
        <WorldFeed />
        <ExchangeTicker />
        <ProvidersPanel />
        <ConversationsLog />
        <QuotaIndicator />
      </>
    );
  }

  const activeAgents = data.agents.filter((a) => a.is_active);
  const inactiveAgents = data.agents.filter((a) => !a.is_active);

  return (
    <>
      <div className="flex items-center justify-between mb-3">
        <h2 className="font-display text-lg text-brown-200 tracking-wider">
          Your Agents ({activeAgents.length})
        </h2>
        <button
          onClick={onJoinClick}
          disabled={joinDisabled}
          title={joinDisabled ? joinDisabledReason : undefined}
          className="px-2 py-1 bg-clay-700 hover:bg-clay-600 text-brown-200 rounded text-xs font-body transition-colors disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-clay-700"
        >
          + Add
        </button>
      </div>
      {joinDisabled && (
        <p className="-mt-2 mb-3 font-body text-xs text-yellow-300/80">{joinDisabledReason}</p>
      )}
      <div className="flex flex-col gap-3">
        {activeAgents.map((agent) => (
          <ApartmentCard
              key={agent.agent_id}
              agent={agent}
              onChatClick={onAgentChatClick}
              onRemove={handleRemove}
              onReactivate={handleReactivate}
            />
        ))}
      </div>
      {inactiveAgents.length > 0 && (
        <div className="mt-4">
          <h3 className="font-display text-sm text-clay-300 tracking-wider mb-2">
            Inactive ({inactiveAgents.length})
          </h3>
          <div className="flex flex-col gap-3">
            {inactiveAgents.map((agent) => (
              <ApartmentCard
              key={agent.agent_id}
              agent={agent}
              onChatClick={onAgentChatClick}
              onRemove={handleRemove}
              onReactivate={handleReactivate}
            />
            ))}
          </div>
        </div>
      )}
      <WorldFeed />
      <ExchangeTicker />
      <ProvidersPanel />
      <ConversationsLog />
      <QuotaIndicator />
    </>
  );
}

export default function App() {
  const [joinModalOpen, setJoinModalOpen] = useState(false);
  const [chatAgent, setChatAgent] = useState<{ id: string; name: string } | null>(null);
  const location = useLocation();
  const navigate = useNavigate();

  const { isSignedIn, isLoaded: authLoaded } = useAuth();

  // Previously listened for ``goosetown_scene_changed`` (dispatched by
  // useTownState on every ``agent_context_changed`` WS broadcast) and
  // auto-navigated the route to match. That made every viewer's URL
  // flip from /town to /apartment whenever any agent (theirs or another
  // user's) crossed the boundary. The dispatch is gone; route changes
  // are user-driven via React Router only.

  // View in Town focus handler — when ?focus=<name> is present on /, pan the
  // camera to that agent. Retries for a few seconds to handle the case where
  // the Godot scene is still switching from apartment to town.
  useEffect(() => {
    if (location.pathname !== '/town') return;
    const params = new URLSearchParams(location.search);
    const focus = params.get('focus');
    if (!focus) return;

    let cancelled = false;
    const tryFocus = (attempt: number): void => {
      if (cancelled) return;
      const w = window as unknown as Record<string, unknown>;
      const positions = w._goosetown_town_agents as
        | Record<string, { x: number; y: number; display: string }>
        | undefined;
      const bridge = w._godotBridge as { setCamera?: (x: number, y: number, zoom: number) => void } | undefined;
      const agent = positions?.[focus.toLowerCase()];
      if (agent && bridge?.setCamera) {
        bridge.setCamera(agent.x, agent.y, 2.5);
        console.log('[GooseTown] focused camera on', agent.display, 'at', agent.x, agent.y);
        return;
      }
      if (attempt < 50) {
        setTimeout(() => tryFocus(attempt + 1), 300);
      } else {
        toast.warn(`${focus} isn't currently in town`);
        navigate('/town', { replace: true });
      }
    };
    // Small initial delay so scene switch can complete first
    const timer = setTimeout(() => tryFocus(0), 200);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [location.pathname, location.search]);

  const scene = useMemo(
    () => sceneForPath(location.pathname, !!isSignedIn),
    [location.pathname, isSignedIn],
  );

  // Landing route: signed-out visitors see the landing page; signed-in
  // visitors are redirected to /town so they don't land here every visit.
  // While Clerk is still resolving the session, render a neutral clay
  // backdrop instead of Landing — otherwise signed-in users see the
  // landing page flash for ~1s before the redirect (isSignedIn is
  // undefined until ``authLoaded`` flips to true).
  if (location.pathname === '/') {
    if (!authLoaded) {
      return <div className="fixed inset-0 bg-clay-900" aria-hidden />;
    }
    return (
      <>
        {isSignedIn
          ? <Navigate to="/town" replace />
          : <Landing />}
        <ToastContainer position="bottom-right" autoClose={2000} closeOnClick theme="dark" />
      </>
    );
  }

  return (
    <main className="relative flex flex-col h-[100dvh] w-screen overflow-hidden bg-clay-900 font-body">
      <TownNav />

      <div className="relative flex-1 overflow-hidden">
        <GameLayout sidebar={<AgentsSidebar onJoinClick={() => setJoinModalOpen(true)} onAgentChatClick={(id, name) => setChatAgent({ id, name })} />}>
          {/* Persistent Godot canvas — never unmounts, switches scenes via JS bridge */}
          <div className="absolute inset-0">
            <GodotCanvas scene={scene} />
          </div>

          {/* Route-specific overlays (if any) render on top */}
          <Routes>
            {SCENES.map(s => (
              <Route key={s.id} path={s.route} element={<InteriorRoute scene={s} />} />
            ))}
            <Route path="/sign-in/*" element={<SignInPage />} />
            <Route path="/sign-up/*" element={<SignUpPage />} />
          </Routes>
        </GameLayout>

        {/* Floating controls -- top-right overlay on the map */}
        <div className="absolute top-4 right-4 z-10 flex items-center gap-3">
          <MusicButton />
          <SignedIn>
            <UserButton afterSignOutUrl="/" />
          </SignedIn>
          <SignedOut>
            <LoginButton />
          </SignedOut>
        </div>
      </div>

      <JoinTownModal open={joinModalOpen} onClose={() => setJoinModalOpen(false)} />
      {chatAgent && (
        <OwnerChatPanel
          agentId={chatAgent.id}
          agentName={chatAgent.name}
          onClose={() => setChatAgent(null)}
        />
      )}
      <ToastContainer position="bottom-right" autoClose={2000} closeOnClick theme="dark" />
    </main>
  );
}
