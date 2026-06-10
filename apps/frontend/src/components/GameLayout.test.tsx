import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect } from 'vitest';
import GameLayout from './GameLayout';

describe('GameLayout mobile drawer', () => {
  it('renders an Agents toggle that opens and closes the drawer', async () => {
    const user = userEvent.setup();
    render(
      <GameLayout sidebar={<div>SIDEBAR CONTENT</div>}>
        <div>MAP</div>
      </GameLayout>,
    );

    const toggle = screen.getByRole('button', { name: /agents/i });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');

    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');

    await user.click(screen.getByRole('button', { name: /close agents panel/i }));
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
  });

  it('always renders sidebar content (CSS hides it on mobile, not React)', () => {
    render(
      <GameLayout sidebar={<div>SIDEBAR CONTENT</div>}>
        <div>MAP</div>
      </GameLayout>,
    );
    expect(screen.getByText('SIDEBAR CONTENT')).toBeInTheDocument();
  });

  it('can reopen the drawer after closing it', async () => {
    const user = userEvent.setup();
    render(
      <GameLayout sidebar={<div>SIDEBAR</div>}>
        <div>MAP</div>
      </GameLayout>,
    );
    const toggle = screen.getByRole('button', { name: /agents/i });
    await user.click(toggle);
    await user.click(screen.getByRole('button', { name: /close agents panel/i }));
    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
  });

  it('closes the drawer when the backdrop is clicked', async () => {
    const user = userEvent.setup();
    render(
      <GameLayout sidebar={<div>SIDEBAR</div>}>
        <div>MAP</div>
      </GameLayout>,
    );
    const toggle = screen.getByRole('button', { name: /agents/i });
    await user.click(toggle);
    expect(screen.getByTestId('drawer-backdrop')).toBeInTheDocument();
    await user.click(screen.getByTestId('drawer-backdrop'));
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByTestId('drawer-backdrop')).not.toBeInTheDocument();
  });

  it('closes the drawer on Escape', async () => {
    const user = userEvent.setup();
    render(
      <GameLayout sidebar={<div>SIDEBAR</div>}>
        <div>MAP</div>
      </GameLayout>,
    );
    const toggle = screen.getByRole('button', { name: /agents/i });
    await user.click(toggle);
    await user.keyboard('{Escape}');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
  });

  it('wires aria-controls from the toggle to the sidebar container', () => {
    render(
      <GameLayout sidebar={<div>SIDEBAR</div>}>
        <div>MAP</div>
      </GameLayout>,
    );
    const toggle = screen.getByRole('button', { name: /agents/i });
    expect(toggle).toHaveAttribute('aria-controls', 'agents-sidebar');
    expect(document.getElementById('agents-sidebar')).not.toBeNull();
  });
});
