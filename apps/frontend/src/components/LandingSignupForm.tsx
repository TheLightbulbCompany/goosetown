import { useState } from "react";
import { Link } from "react-router-dom";

type Status =
  | "idle"
  | "submitting"
  | "success"
  | "already_subscribed"
  | "unavailable"
  | "error";

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
        const body = await response.json().catch(() => ({}));
        const result = (body as { status?: string }).status;
        setStatus(result === "already_subscribed" ? "already_subscribed" : "success");
        setEmail("");
      } else if (response.status === 503) {
        // Backend reports the signup pipeline isn't ready (Resend audience
        // not yet provisioned, or upstream Resend outage). Don't show a
        // scary error — let the user know they can still watch the town.
        setStatus("unavailable");
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
    <form onSubmit={handleSubmit} noValidate className="w-full max-w-sm">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-stretch">
        <input
          type="email"
          required
          placeholder="email@example.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          disabled={
            status === "submitting" ||
            status === "success" ||
            status === "already_subscribed" ||
            status === "unavailable"
          }
          aria-label="Email address"
          className="flex-1 rounded-md border-2 border-clay-700 bg-clay-900 px-4 py-3 font-body text-sm text-brown-100 placeholder-clay-300 transition focus:border-brown-300 focus:outline-none disabled:opacity-50"
        />

        {/* Honeypot — hidden from users, only filled by bots. */}
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
          disabled={
            status === "submitting" ||
            status === "success" ||
            status === "already_subscribed" ||
            status === "unavailable"
          }
          className="rounded-md border border-brown-300/40 bg-brown-700/80 px-5 py-3 font-display text-sm tracking-widest uppercase text-brown-100 shadow-[0_3px_0_0_rgba(0,0,0,0.4)] transition hover:bg-brown-700 hover:translate-y-[-1px] hover:shadow-[0_4px_0_0_rgba(0,0,0,0.4)] active:translate-y-[1px] active:shadow-[0_1px_0_0_rgba(0,0,0,0.4)] disabled:opacity-50 disabled:hover:translate-y-0 disabled:hover:shadow-[0_3px_0_0_rgba(0,0,0,0.4)]"
        >
          {status === "submitting" ? "Sending…" : "Notify me"}
        </button>
      </div>

      {status === "success" && (
        <div className="mt-4 text-center">
          <p className="font-body text-sm text-green-300">
            Got it — we&apos;ll be in touch when the public beta opens.
          </p>
          <Link
            to="/town"
            className="mt-3 inline-flex items-center gap-1 font-body text-sm text-clay-100 underline underline-offset-4 hover:text-brown-200"
          >
            Want to peek at the town now?
            <span aria-hidden>→</span>
          </Link>
        </div>
      )}
      {status === "already_subscribed" && (
        <div className="mt-4 text-center">
          <p className="font-body text-sm text-clay-100">
            You&apos;re already on the list — we&apos;ll be in touch when the public
            beta opens.
          </p>
          <Link
            to="/town"
            className="mt-3 inline-flex items-center gap-1 font-body text-sm text-clay-100 underline underline-offset-4 hover:text-brown-200"
          >
            Take another look at the town
            <span aria-hidden>→</span>
          </Link>
        </div>
      )}
      {status === "unavailable" && (
        <div className="mt-4 text-center">
          <p className="font-body text-sm text-clay-100">
            Email signups aren&apos;t enabled yet — but you can still walk through
            the town.
          </p>
          <Link
            to="/town"
            className="mt-3 inline-flex items-center gap-1 font-body text-sm text-clay-100 underline underline-offset-4 hover:text-brown-200"
          >
            Visit the town live
            <span aria-hidden>→</span>
          </Link>
        </div>
      )}
      {status === "error" && (
        <p className="mt-3 font-body text-sm text-red-300">{errorMsg}</p>
      )}
    </form>
  );
}
