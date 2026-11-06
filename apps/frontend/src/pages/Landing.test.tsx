import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { Landing } from "./Landing";

describe("Landing", () => {
  beforeEach(() => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ status: "ok" }), { status: 200 }),
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders hero, signup form, and an explicit Visit-the-Town link", () => {
    render(
      <MemoryRouter>
        <Landing />
      </MemoryRouter>,
    );

    // Hero title appears as a top-level heading.
    expect(screen.getByRole("heading", { level: 1, name: /goosetown/i })).toBeInTheDocument();

    // Signup form is reachable via the email input.
    expect(screen.getByPlaceholderText(/email/i)).toBeInTheDocument();

    // Primary CTA: a link to /town. Anonymous viewing is the killer feature
    // and lives above the fold — must always be present and routed correctly.
    const visitLink = screen.getByRole("link", { name: /visit the town/i });
    expect(visitLink).toHaveAttribute("href", "/town");

    // No TownProvider state = 0 agents: never claim "live now".
    expect(screen.queryByText(/live now/i)).toBeNull();
  });
});
