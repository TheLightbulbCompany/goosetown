import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import MusicButton from './MusicButton';

// Track play/pause calls on any Audio instance created during the test.
// The component instantiates `new Audio(url)` lazily on first press, so we
// stub the constructor globally and capture the mock instance.
function installAudioStub() {
  const pause = vi.fn();
  const play = vi.fn().mockResolvedValue(undefined);
  const AudioStub = vi.fn().mockImplementation(() => ({
    pause,
    play,
    loop: false,
  }));
  // Replace the global Audio constructor for the test.
  (globalThis as unknown as { Audio: unknown }).Audio = AudioStub;
  return { play, pause, ctor: AudioStub };
}

describe('MusicButton — keyboard shortcut lifecycle', () => {
  beforeEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('toggles play/pause on repeated M presses (no stale closure)', async () => {
    // Regression test for the stale-closure concern: ``handleKeyPress``
    // must read the LATEST ``isPlaying`` on every keypress, not the
    // initial render's value. If broken, the second M press would call
    // ``play`` again instead of ``pause`` (or vice versa).
    const { play, pause } = installAudioStub();
    const user = userEvent.setup();

    render(<MusicButton />);

    // Press M once — should call play().
    await user.keyboard('{m}');
    expect(play).toHaveBeenCalledTimes(1);
    expect(pause).toHaveBeenCalledTimes(0);

    // Press M again — should call pause(), NOT play() again. Under a
    // stale closure, ``flipSwitch`` would still see ``isPlaying=false``
    // and call play() a second time instead of pause().
    await user.keyboard('{m}');
    expect(play).toHaveBeenCalledTimes(1);
    expect(pause).toHaveBeenCalledTimes(1);

    // Press M a third time — should play again.
    await user.keyboard('{m}');
    expect(play).toHaveBeenCalledTimes(2);
    expect(pause).toHaveBeenCalledTimes(1);

    // Fourth time — pause again.
    await user.keyboard('{m}');
    expect(play).toHaveBeenCalledTimes(2);
    expect(pause).toHaveBeenCalledTimes(2);
  });

  it('uppercase M also toggles', async () => {
    const { play, pause } = installAudioStub();
    const user = userEvent.setup();
    render(<MusicButton />);
    await user.keyboard('{Shift>}M{/Shift}');
    expect(play).toHaveBeenCalledTimes(1);
    await user.keyboard('{Shift>}M{/Shift}');
    expect(pause).toHaveBeenCalledTimes(1);
  });

  it('button label flips between "Music" and "Mute" as state toggles', async () => {
    installAudioStub();
    const user = userEvent.setup();
    render(<MusicButton />);
    // The shared Button component renders an <a>, not a <button>, so
    // look up by label text rather than role.
    expect(screen.getByText('Music')).toBeInTheDocument();
    await user.keyboard('{m}');
    expect(screen.getByText('Mute')).toBeInTheDocument();
    await user.keyboard('{m}');
    expect(screen.getByText('Music')).toBeInTheDocument();
  });

  it('removes the keydown listener on unmount', async () => {
    const { play } = installAudioStub();
    const user = userEvent.setup();
    const { unmount } = render(<MusicButton />);
    unmount();
    // After unmount, M-key presses must not reach the handler (no play).
    await user.keyboard('{m}');
    expect(play).toHaveBeenCalledTimes(0);
  });
});
