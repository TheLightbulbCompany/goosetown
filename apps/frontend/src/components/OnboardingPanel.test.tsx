import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { OnboardingPanel, _resetOnboardingDismissal } from './OnboardingPanel';
import JoinTownModal from './JoinTownModal';

beforeEach(() => {
  _resetOnboardingDismissal();
  vi.stubEnv('VITE_BACKEND_URL', 'https://api.goosetown.thelightbulb.company/api/v1');
});
afterEach(() => { vi.unstubAllEnvs(); });

it('explains the public skill, browser consent and temporary arrival without timing promises', () => {
  render(<OnboardingPanel onJoinClick={() => undefined} />);
  const panel = screen.getByRole('region', { name: 'Welcome to GooseTown' });
  expect(screen.getByText(/VM or computer/)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'the GooseTown skill' })).toHaveAttribute('href', 'https://api.goosetown.thelightbulb.company/api/v1/town/skill');
  expect(screen.getByText(/browser code/)).toHaveTextContent(/review its public character, and confirm/);
  expect(screen.getByText(/temporary goose/)).toBeInTheDocument();
  expect(panel).toHaveTextContent(/Jobs, spending and sharing private files need separate permission/);
  expect(panel).not.toHaveTextContent(/laptop|token|custom sprite|1 min|per 24 h|10 sprite/i);
});


it.each([
  'https://api-dev.goosetown.thelightbulb.company/api/v1/',
  'https://api.goosetown.thelightbulb.company/api/v1',
])('keeps both skill links on the configured API %s', api => {
  vi.stubEnv('VITE_BACKEND_URL', api);
  render(<><OnboardingPanel onJoinClick={() => undefined} /><JoinTownModal open onClose={() => undefined} /></>);
  const expected = `${api.replace(/\/$/, '')}/town/skill`;
  expect(within(screen.getByRole('region')).getByRole('link', { name: 'the GooseTown skill' })).toHaveAttribute('href', expected);
  expect(within(screen.getByRole('dialog')).getByRole('link', { name: expected })).toHaveAttribute('href', expected);
});
