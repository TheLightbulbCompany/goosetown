/**
 * ProvidersPanel — the town service directory, read-only.
 *
 * An owner browsing who's offering services in town: each provider is a small
 * directory "shingle" — name in font-display, a trust **stamp chip** reusing
 * the passbook's NEWCOMER/reliable/trusted treatment, skills as small bordered
 * chips, bio in font-body. A category filter chip row at the top scopes the
 * directory. No crumbs/PII — providers don't expose a wallet here.
 */

import { useState } from 'react';
import { useProviders } from '../hooks/useProviders.ts';
import type { Provider } from '../hooks/useProviders.ts';

/** Category filter chips — a small fixed set from the AEX seed domains, plus
 * an "all" sentinel that clears the filter. (Slugs match the backend's
 * SEED_DOMAINS; the model is open, these are the onboarding-UI suggestions.) */
const CATEGORIES = ['writing', 'research', 'technology', 'creative', 'conversation'] as const;
const ALL = 'all';

/** Trust stamp chip — the same bordered, lowercase, font-display treatment the
 * passbook stamps the tier with. Pure presentation, no PII. */
function TrustStamp({ tier }: { tier: string }) {
  return (
    <span
      className="font-display lowercase text-[10px] tracking-wider border border-clay-500 text-clay-100 px-1 shrink-0"
      data-testid="trust-stamp"
    >
      {tier}
    </span>
  );
}

/** A single skill rendered as a small bordered chip. */
function SkillChip({ skill }: { skill: string }) {
  return (
    <span
      className="font-body text-[10px] text-brown-700 border border-brown-300 px-1 break-words"
      data-testid="skill-chip"
    >
      {skill}
    </span>
  );
}

/** One provider directory shingle. */
function ProviderShingle({ provider }: { provider: Provider }) {
  return (
    <div className="bg-brown-200 p-3">
      <div className="flex items-center gap-2">
        <h4 className="font-display text-sm text-brown-900 tracking-wider break-words flex-1 min-w-0">
          {provider.name || 'someone in town'}
        </h4>
        <TrustStamp tier={provider.trust_tier} />
      </div>
      {provider.skills.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-1.5">
          {provider.skills.map((s) => (
            <SkillChip key={s} skill={s} />
          ))}
        </div>
      )}
      {provider.bio && (
        <p className="font-body text-xs text-clay-300 mt-1.5 break-words">{provider.bio}</p>
      )}
      <p className="font-body text-[11px] text-brown-700 mt-1.5">
        {provider.jobs_completed} job{provider.jobs_completed === 1 ? '' : 's'} done
      </p>
    </div>
  );
}

/** A selectable category filter chip. */
function CategoryChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`font-body lowercase text-[10px] tracking-wider border px-1.5 py-0.5 transition-colors ${
        active
          ? 'border-brown-200 text-brown-200 bg-clay-700'
          : 'border-clay-700 text-clay-300 hover:text-clay-100 hover:border-clay-500'
      }`}
    >
      {label}
    </button>
  );
}

export function ProvidersPanel() {
  const [open, setOpen] = useState(true);
  // ``selected`` is the active category slug, or null when "all" is selected.
  const [selected, setSelected] = useState<string | null>(null);
  const { providers, loading, error } = useProviders(selected);

  return (
    <div className="mt-6">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full text-left font-display text-sm text-clay-300 hover:text-clay-100 tracking-wider mb-2 flex items-center gap-1"
        aria-expanded={open}
      >
        <span aria-hidden="true">{open ? '▾' : '▸'}</span>
        PROVIDERS
      </button>
      {open && (
        <>
          {/* Category filter chip row — "all" clears the filter. */}
          <div className="flex flex-wrap gap-1 mb-2">
            <CategoryChip
              label={ALL}
              active={selected === null}
              onClick={() => setSelected(null)}
            />
            {CATEGORIES.map((cat) => (
              <CategoryChip
                key={cat}
                label={cat}
                active={selected === cat}
                onClick={() => setSelected(cat)}
              />
            ))}
          </div>

          {loading && providers.length === 0 && (
            <p className="text-clay-500 text-xs font-body italic">Asking around town…</p>
          )}
          {error && !loading && (
            <p className="text-red-400 text-xs font-body">Directory error: {error}</p>
          )}
          {!loading && !error && providers.length === 0 && (
            <div className="bg-brown-200 p-3">
              <p className="font-body text-xs text-brown-700 italic">
                No providers yet — be the first to offer a service.
              </p>
            </div>
          )}
          {providers.length > 0 && (
            <div className="flex flex-col gap-2 max-h-[40vh] overflow-y-auto pr-1">
              {providers.map((p) => (
                <ProviderShingle key={p.agent_id} provider={p} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default ProvidersPanel;
