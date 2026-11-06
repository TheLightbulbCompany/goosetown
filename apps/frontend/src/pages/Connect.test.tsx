import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import Connect from './Connect';
import JoinTownModal from '../components/JoinTownModal';

vi.mock('@clerk/clerk-react', () => ({
  useAuth: () => ({ isLoaded: true, isSignedIn: true, getToken: async () => 'owner-session' }),
  SignIn: () => <div>Sign in with Lightbulb</div>,
}));
beforeEach(() => { vi.stubEnv('VITE_BACKEND_URL', 'https://api.goosetown.thelightbulb.company/api/v1'); });
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllEnvs(); });

it('shows the public character and sends approval without exposing a credential', async () => {
  window.history.replaceState({}, '', '/connect?code=ABCDE-12345');
  const fetch = vi.spyOn(global, 'fetch').mockImplementation(async (_url, init) => new Response(JSON.stringify(
    init?.method === 'POST' ? { state: 'approved' } : {
      state: 'pending', persona: { agent_name: 'lucky', appearance: 'Blue scarf', personality: 'Kind' },
    }), { status: 200 }));
  render(<Connect />);
  expect(await screen.findByText('lucky', { selector: 'strong' })).toBeInTheDocument();
  expect(screen.getByText('Blue scarf')).toBeInTheDocument();
  expect(screen.getByText(/Jobs, spending and sharing private files need separate permission/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Join GooseTown' }));
  await waitFor(() => expect(screen.getByText(/lucky is connecting to GooseTown/i)).toBeInTheDocument());
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/ABCDE-12345/approve'), expect.objectContaining({ method: 'POST' }));
  expect(screen.queryByText(/token|gtj_|being drawn|1 min/i)).toBeNull();
  expect(screen.getByText(/temporary goose/)).toBeInTheDocument();
});

it('opening join instructions does not mint or fetch a token', () => {
  const fetch = vi.spyOn(global, 'fetch');
  render(<JoinTownModal open onClose={() => undefined} />);
  expect(screen.getByRole('link', { name: 'https://api.goosetown.thelightbulb.company/api/v1/town/skill' })).toHaveAttribute('href', 'https://api.goosetown.thelightbulb.company/api/v1/town/skill');
  expect(screen.getByText(/VM or computer/)).toBeInTheDocument();
  expect(fetch).not.toHaveBeenCalled();
});
