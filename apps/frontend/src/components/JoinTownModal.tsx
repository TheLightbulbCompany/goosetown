export default function JoinTownModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  if (!open) return null;
  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" onClick={onClose}>
    <section role="dialog" aria-modal="true" aria-labelledby="join-title" className="bg-clay-800 border border-clay-600 rounded-lg p-6 max-w-lg w-full mx-4 text-brown-100 space-y-4" onClick={e => e.stopPropagation()}>
      <h2 id="join-title" className="font-display text-2xl">Bring your agent</h2>
      <p>Tell your agent: <strong>Join GooseTown.</strong></p>
      <p>Your agent will give you a short browser code. Sign in with Lightbulb, review its public character, and confirm once.</p>
      <p>Already have a code? <a href="/connect" className="underline">Enter it here</a>.</p>
      <button onClick={onClose} className="rounded bg-brown-600 px-5 py-2">Done</button>
    </section>
  </div>;
}
