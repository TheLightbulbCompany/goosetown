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
});
