import { useEffect, useState } from 'react';
import { Routes, Route, useLocation, useNavigate } from 'react-router-dom';
import { SignedIn, SignedOut, UserButton, useAuth } from '@clerk/clerk-react';
import { ToastContainer, toast } from 'react-toastify';
import TownNav from './components/TownNav.tsx';
import GameLayout from './components/GameLayout.tsx';
import GodotCanvas from './components/GodotCanvas.tsx';
import ApartmentCard from './components/ApartmentCard.tsx';
import ConversationsLog from './components/ConversationsLog.tsx';
import LoginButton from './components/buttons/LoginButton.tsx';
import MusicButton from './components/buttons/MusicButton.tsx';
import JoinTownModal from './components/JoinTownModal.tsx';
import { useApartment } from './hooks/useApartment.ts';
import Town from './pages/Town.tsx';
import Apartment from './pages/Apartment.tsx';
import SignInPage from './pages/SignIn.tsx';
import SignUpPage from './pages/SignUp.tsx';

function AgentsSidebar({ onJoinClick }: { onJoinClick: () => void }) {
  const { data, loading } = useApartment();

  if (loading) {
    return <p className="font-body text-clay-300 text-sm p-2">Loading agents...</p>;
  }

  if (!data || data.agents.length === 0) {
    return (
      <>
        <SignedIn>
          <div className="p-2 space-y-3">
            <p className="font-body text-clay-300 text-sm">No agents in town yet.</p>
            <button
              onClick={onJoinClick}
              className="w-full px-4 py-2 bg-brown-600 hover:bg-brown-500 text-brown-100 rounded font-body text-sm transition-colors"
            >
              Join GooseTown
            </button>
          </div>
        </SignedIn>
        <ConversationsLog />
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
          className="px-2 py-1 bg-clay-700 hover:bg-clay-600 text-brown-200 rounded text-xs font-body transition-colors"
        >
          + Add
        </button>
      </div>
      <div className="flex flex-col gap-3">
        {activeAgents.map((agent) => (
          <ApartmentCard key={agent.agent_id} agent={agent} />
        ))}
      </div>
      {inactiveAgents.length > 0 && (
        <div className="mt-4">
          <h3 className="font-display text-sm text-clay-300 tracking-wider mb-2">
            Inactive ({inactiveAgents.length})
          </h3>
          <div className="flex flex-col gap-3">
            {inactiveAgents.map((agent) => (
              <ApartmentCard key={agent.agent_id} agent={agent} />
            ))}
          </div>
        </div>
      )}
      <ConversationsLog />
    </>
  );
}

export default function App() {
  const [joinModalOpen, setJoinModalOpen] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();

  const { isSignedIn } = useAuth();

  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      const target = detail.scene === 'apartment' ? '/apartment' : '/';
      if (location.pathname !== target) {
        navigate(target);
      }
    };
    window.addEventListener('goosetown_scene_changed', handler);
    return () => window.removeEventListener('goosetown_scene_changed', handler);
  }, [location.pathname, navigate]);

  // View in Town focus handler — when ?focus=<name> is present on /, pan the
  // camera to that agent. Retries for a few seconds to handle the case where
  // the Godot scene is still switching from apartment to town.
  useEffect(() => {
    if (location.pathname !== '/') return;
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
        navigate('/', { replace: true });
      }
    };
    // Small initial delay so scene switch can complete first
    const timer = setTimeout(() => tryFocus(0), 200);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [location.pathname, location.search]);

  // Only switch to apartment if authenticated AND on apartment route
  const scene: 'town' | 'apartment' =
    location.pathname === '/apartment' && isSignedIn ? 'apartment' : 'town';

  return (
    <main className="relative flex flex-col h-screen w-screen overflow-hidden bg-clay-900 font-body">
      <TownNav />

      <div className="relative flex-1 overflow-hidden">
        <GameLayout sidebar={<AgentsSidebar onJoinClick={() => setJoinModalOpen(true)} />}>
          {/* Persistent Godot canvas — never unmounts, switches scenes via JS bridge */}
          <div className="absolute inset-0">
            <GodotCanvas scene={scene} />
          </div>

          {/* Route-specific overlays (if any) render on top */}
          <Routes>
            <Route path="/" element={<Town />} />
            <Route path="/apartment" element={<Apartment />} />
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
      <ToastContainer position="bottom-right" autoClose={2000} closeOnClick theme="dark" />
    </main>
  );
}
