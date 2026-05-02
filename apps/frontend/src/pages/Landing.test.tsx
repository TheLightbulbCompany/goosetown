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

  it("renders hero copy and the signup form", () => {
    render(
      <MemoryRouter>
        <Landing />
      </MemoryRouter>,
    );
    // Hero / tagline contains the project name
    expect(screen.getByText(/goosetown/i)).toBeInTheDocument();
    // Email input from LandingSignupForm
    expect(screen.getByPlaceholderText(/email/i)).toBeInTheDocument();
    // Town link
    const link = screen.getByRole("link", { name: /(see|town|live)/i });
    expect(link).toHaveAttribute("href", "/town");
  });
});
