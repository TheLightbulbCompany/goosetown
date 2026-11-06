import { beforeEach, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { OnboardingPanel, _resetOnboardingDismissal } from './OnboardingPanel';

beforeEach(() => { _resetOnboardingDismissal(); });

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

