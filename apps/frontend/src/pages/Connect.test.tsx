import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
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

it.each([404, 410])('stops arrival polling on terminal %s and accepts a fresh code', async status => {
  vi.useFakeTimers();
  window.history.replaceState({}, '', '/connect?code=OLD-CODE');
  const persona = { agent_name: 'lucky', appearance: 'Blue scarf', personality: 'Kind' };
  const fetch = vi.spyOn(global, 'fetch')
    .mockResolvedValueOnce(new Response(JSON.stringify({ state: 'approved', persona }), { status: 200 }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ detail: 'Request unavailable' }), { status }))
    .mockResolvedValue(new Response(JSON.stringify({ state: 'pending', persona }), { status: 200 }));
  try {
    render(<Connect />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText(/lucky is connecting/i)).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByRole('alert')).toHaveTextContent(/code expired or is no longer available/i);
    expect(screen.queryByText(/lucky is connecting/i)).toBeNull();
    const callsAtExpiry = fetch.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(fetch).toHaveBeenCalledTimes(callsAtExpiry);
    fireEvent.click(screen.getByRole('button', { name: 'Enter a new code' }));
    expect(window.location.search).toBe('');
    fireEvent.change(screen.getByRole('textbox', { name: 'Code from your agent' }), { target: { value: 'FRESH-CODE' } });
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText('lucky', { selector: 'strong' })).toBeInTheDocument();
    expect(fetch).toHaveBeenLastCalledWith(expect.stringContaining('/FRESH-CODE'), expect.any(Object));
  } finally { vi.useRealTimers(); }
});

it.each(['server', 'network'])('continues polling after a transient %s failure', async failure => {
  vi.useFakeTimers();
  window.history.replaceState({}, '', '/connect?code=RETRY-CODE');
  const persona = { agent_name: 'lucky', appearance: 'Blue scarf', personality: 'Kind' };
  const fetch = vi.spyOn(global, 'fetch')
    .mockResolvedValueOnce(new Response(JSON.stringify({ state: 'approved', persona }), { status: 200 }));
  if (failure === 'server') fetch.mockResolvedValueOnce(new Response('Temporarily unavailable', { status: 503 }));
  else fetch.mockRejectedValueOnce(new TypeError('Network unavailable'));
  fetch.mockResolvedValue(new Response(JSON.stringify({ state: 'consumed', persona }), { status: 200 }));
  try {
    render(<Connect />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByText(/lucky is connecting/i)).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(fetch).toHaveBeenCalledTimes(3);
    expect(screen.getByText(/lucky is connecting/i)).toBeInTheDocument();
  } finally { vi.useRealTimers(); }
});
