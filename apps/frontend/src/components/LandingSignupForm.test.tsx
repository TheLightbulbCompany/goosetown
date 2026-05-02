import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { LandingSignupForm } from "./LandingSignupForm";

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
    render(<LandingSignupForm />);
    expect(screen.getByPlaceholderText(/email/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /(notify|sign up|join)/i })).toBeInTheDocument();
  });

  it("submits to /landing/signup on click", async () => {
    render(<LandingSignupForm />);
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

  it("shows success message on 200", async () => {
    render(<LandingSignupForm />);
    fireEvent.change(screen.getByPlaceholderText(/email/i), {
      target: { value: "user@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /(notify|sign up|join)/i }));

    expect(await screen.findByText(/(thanks|got it|added|launch)/i)).toBeInTheDocument();
  });

  it("shows error message on non-200", async () => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Invalid email" }), { status: 400 }),
    );
    render(<LandingSignupForm />);
    fireEvent.change(screen.getByPlaceholderText(/email/i), {
      target: { value: "bad" },
    });
    fireEvent.click(screen.getByRole("button", { name: /(notify|sign up|join)/i }));

    expect(await screen.findByText(/(error|invalid|try again|could not)/i)).toBeInTheDocument();
  });
});
