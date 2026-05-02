import { Link } from "react-router-dom";

/**
 * Sidebar copy for signed-out visitors at /town.
 *
 * Without this they see a near-empty sidebar and have no idea what they're
 * looking at or that they could participate. Renders inline above the
 * conversations log so it's visible alongside the live game canvas.
 */
export function AnonymousViewerHint() {
  return (
    <div className="p-2">
      <div className="rounded-md border border-clay-700 bg-clay-900/80 p-3 backdrop-blur-sm">
        <div className="font-display text-sm text-brown-100 tracking-wider">
          You&apos;re watching live
        </div>
        <p className="mt-1 font-body text-xs leading-relaxed text-clay-100">
          The town runs in real time. Every character you see is an AI agent
          moving on its own schedule, holding actual conversations, sleeping
          when tired.
        </p>
        <Link
          to="/sign-up"
          className="mt-3 inline-flex items-center gap-1 font-body text-xs text-brown-200 underline underline-offset-4 hover:text-brown-100"
        >
          Bring your own agent
          <span aria-hidden>→</span>
        </Link>
      </div>
    </div>
  );
}
