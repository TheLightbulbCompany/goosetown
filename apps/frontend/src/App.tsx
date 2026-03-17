import { useState } from 'react';
import { Routes, Route } from 'react-router-dom';
import { SignedIn, SignedOut, UserButton } from '@clerk/clerk-react';
import { ToastContainer } from 'react-toastify';
import TownNav from './components/TownNav.tsx';
import GameLayout from './components/GameLayout.tsx';
import ApartmentCard from './components/ApartmentCard.tsx';
import LoginButton from './components/buttons/LoginButton.tsx';
import MusicButton from './components/buttons/MusicButton.tsx';
import JoinTownModal from './components/JoinTownModal.tsx';
import { useApartment } from './hooks/useApartment.ts';
import Town from './pages/Town.tsx';
import Apartment from './pages/Apartment.tsx';

function AgentsSidebar({ onJoinClick }: { onJoinClick: () => void }) {
  const { data, loading } = useApartment();

  if (loading) {
    return <p className="font-body text-clay-300 text-sm p-2">Loading agents...</p>;
  }

  if (!data || data.agents.length === 0) {
    return (
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
    </>
  );
}

export default function App() {
  const [joinModalOpen, setJoinModalOpen] = useState(false);

  return (
    <main className="relative flex flex-col h-screen w-screen overflow-hidden bg-clay-900 font-body">
      <TownNav />

      <div className="relative flex-1 overflow-hidden">
        <GameLayout sidebar={<AgentsSidebar onJoinClick={() => setJoinModalOpen(true)} />}>
          <Routes>
            <Route path="/" element={<Town />} />
            <Route path="/apartment" element={<Apartment />} />
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
