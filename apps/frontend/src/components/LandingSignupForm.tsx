import { useState } from "react";

type Status = "idle" | "submitting" | "success" | "error";

export function LandingSignupForm() {
  const [email, setEmail] = useState("");
  const [honeypot, setHoneypot] = useState("");
  const [status, setStatus] = useState<Status>("idle");
  const [errorMsg, setErrorMsg] = useState("");

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setStatus("submitting");
    setErrorMsg("");

    try {
      const response = await fetch(
        `${import.meta.env.VITE_BACKEND_URL}/landing/signup`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email, honeypot: honeypot || undefined }),
        },
      );

      if (response.ok) {
        setStatus("success");
        setEmail("");
      } else {
        const body = await response.json().catch(() => ({}));
        setStatus("error");
        setErrorMsg((body as { detail?: string }).detail || "Could not sign up — please try again");
      }
    } catch {
      setStatus("error");
      setErrorMsg("Network error — please try again");
    }
  }

  return (
    <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-2 max-w-sm">
      <input
        type="email"
        required
        placeholder="email@example.com"
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        disabled={status === "submitting"}
        className="rounded border border-clay-600 bg-brown-900 px-3 py-2 font-body text-sm text-brown-100 placeholder-clay-500 focus:outline-none focus:border-clay-400 disabled:opacity-50"
      />
      {/* Honeypot: hidden from users, only filled by bots */}
      <input
        type="text"
        name="website"
        value={honeypot}
        onChange={(e) => setHoneypot(e.target.value)}
        tabIndex={-1}
        aria-hidden="true"
        autoComplete="off"
        style={{ position: "absolute", left: "-9999px" }}
      />
      <button
        type="submit"
        disabled={status === "submitting"}
        className="rounded bg-clay-700 px-4 py-2 font-body text-sm text-brown-100 hover:bg-clay-600 disabled:opacity-50 transition-colors"
      >
        {status === "submitting" ? "Submitting…" : "Notify me"}
      </button>
      {status === "success" && (
        <p className="font-body text-sm text-green-400">
          Thanks — we got it. You'll hear from us at launch.
        </p>
      )}
      {status === "error" && (
        <p className="font-body text-sm text-red-400">{errorMsg}</p>
      )}
    </form>
  );
}
