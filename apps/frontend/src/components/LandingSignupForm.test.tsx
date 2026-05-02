import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { LandingSignupForm } from "./LandingSignupForm";

function renderForm() {
  // Form's success + unavailable states render <Link to="/town">; tests need
  // a Router context.
  return render(
    <MemoryRouter>
      <LandingSignupForm />
    </MemoryRouter>,
  );
}

describe("LandingSignupForm", () => {
  beforeEach(() => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ status: "ok" }), { status: 200 }),
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders an email input and submit button", () => {
    renderForm();
    expect(screen.getByPlaceholderText(/email/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /(notify|sign up|join)/i })).toBeInTheDocument();
  });

  it("submits to /landing/signup on click", async () => {
    renderForm();
    fireEvent.change(screen.getByPlaceholderText(/email/i), {
      target: { value: "user@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /(notify|sign up|join)/i }));

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/landing/signup"),
        expect.objectContaining({
          method: "POST",
          body: expect.stringContaining("user@example.com"),
        }),
      );
    });
  });

  it("shows success message on 200 and an explicit Visit-the-town link", async () => {
    renderForm();
    fireEvent.change(screen.getByPlaceholderText(/email/i), {
      target: { value: "user@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /(notify|sign up|join)/i }));

    expect(
      await screen.findByText(/(got it|in touch|public beta)/i),
    ).toBeInTheDocument();
    // Success state offers a follow-up to the live town instead of a dead end.
    expect(
      screen.getByRole("link", { name: /(peek|town)/i }),
    ).toHaveAttribute("href", "/town");
  });

  it("shows a graceful unavailable message on 503 (signup pipeline not provisioned)", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Signup temporarily unavailable" }), {
        status: 503,
      }),
    );
    renderForm();
    fireEvent.change(screen.getByPlaceholderText(/email/i), {
      target: { value: "user@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /(notify|sign up|join)/i }));

    // Honest, friendly copy — not a generic "error".
    expect(
      await screen.findByText(/(aren't enabled|isn't enabled|walk through the town)/i),
    ).toBeInTheDocument();
    // Pivot to anonymous viewing.
    expect(
      screen.getByRole("link", { name: /visit the town/i }),
    ).toHaveAttribute("href", "/town");
  });

  it("shows error message on other 4xx", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Invalid email" }), { status: 400 }),
    );
    renderForm();
    fireEvent.change(screen.getByPlaceholderText(/email/i), {
      target: { value: "bad" },
    });
    fireEvent.click(screen.getByRole("button", { name: /(notify|sign up|join)/i }));

    expect(
      await screen.findByText(/(error|invalid|try again|could not)/i),
    ).toBeInTheDocument();
  });
});
