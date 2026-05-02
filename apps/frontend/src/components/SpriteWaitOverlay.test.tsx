import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act } from "@testing-library/react";
import { SpriteWaitOverlay } from "./SpriteWaitOverlay";

describe("SpriteWaitOverlay", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows 'Drawing your agent' with elapsed seconds", () => {
    const createdAt = Date.now() - 15_000; // 15s ago
    render(<SpriteWaitOverlay createdAt={createdAt} />);
    expect(screen.getByText(/drawing/i)).toBeInTheDocument();
    expect(screen.getByText(/15s/)).toBeInTheDocument();
  });

  it("ticks elapsed seconds every second", () => {
    const createdAt = Date.now();
    render(<SpriteWaitOverlay createdAt={createdAt} />);
    expect(screen.getByText(/0s/)).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(3_000);
    });
    expect(screen.getByText(/3s/)).toBeInTheDocument();
  });

  it("flips to a 'still drawing' stuck state after stuckAfterSec elapses", () => {
    // ``stuckAfterSec`` is configurable so tests don't have to wait the
    // production 120s default. 5s here keeps the test fast.
    const createdAt = Date.now();
    render(<SpriteWaitOverlay createdAt={createdAt} stuckAfterSec={5} />);

    // Idle state at t=0.
    expect(screen.getByText(/drawing your agent/i)).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(6_000);
    });

    // Stuck state copy now visible; the original "drawing your agent" copy
    // is gone (we render a different branch).
    expect(screen.getByText(/still drawing/i)).toBeInTheDocument();
    expect(screen.queryByText(/drawing your agent/i)).not.toBeInTheDocument();
    expect(screen.getByText(/unusually slow/i)).toBeInTheDocument();
  });
});
