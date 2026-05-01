import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { RevokeTokenButton } from "./RevokeTokenButton";

describe("RevokeTokenButton", () => {
  beforeEach(() => {
    vi.spyOn(global, "fetch").mockResolvedValue(
      new Response(null, { status: 204 }),
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the revoke button", () => {
    render(<RevokeTokenButton getToken={async () => "fake-jwt"} />);
    expect(screen.getByRole("button", { name: /revoke/i })).toBeInTheDocument();
  });

  it("shows confirmation step when clicked", async () => {
    render(<RevokeTokenButton getToken={async () => "fake-jwt"} />);
    fireEvent.click(screen.getByRole("button", { name: /revoke/i }));
    expect(await screen.findByText(/all your agents/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /confirm/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /cancel/i })).toBeInTheDocument();
  });

  it("calls POST /town/instance/revoke on confirm", async () => {
    const getToken = vi.fn(async () => "fake-jwt");
    render(<RevokeTokenButton getToken={getToken} />);

    fireEvent.click(screen.getByRole("button", { name: /revoke/i }));
    fireEvent.click(await screen.findByRole("button", { name: /confirm/i }));

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        expect.stringContaining("/town/instance/revoke"),
        expect.objectContaining({
          method: "POST",
          headers: expect.objectContaining({ Authorization: "Bearer fake-jwt" }),
        }),
      );
    });

    expect(getToken).toHaveBeenCalled();
  });

  it("cancel returns to initial button without calling fetch", async () => {
    render(<RevokeTokenButton getToken={async () => "fake-jwt"} />);

    fireEvent.click(screen.getByRole("button", { name: /revoke/i }));
    fireEvent.click(await screen.findByRole("button", { name: /cancel/i }));

    // Back to initial state
    expect(screen.getByRole("button", { name: /revoke/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /confirm/i })).not.toBeInTheDocument();
    expect(global.fetch).not.toHaveBeenCalled();
  });
});
