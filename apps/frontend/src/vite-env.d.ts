/// <reference types="vite/client" />

// Typed shape for the env vars the frontend actually uses. Without these
// ambient declarations, ``import.meta.env.VITE_FOO`` is ``any``, which
// forced hooks like ``useTownState`` to ``(import.meta as any).env?.VITE_BACKEND_URL``.
// Declaring them here lets TypeScript narrow the ``import.meta.env`` type
// and the ``as any`` casts can go away.
interface ImportMetaEnv {
  readonly VITE_BACKEND_URL?: string;
  readonly VITE_WS_URL?: string;
  readonly VITE_CLERK_PUBLISHABLE_KEY?: string;
  /** Set by scripts/build-with-godot-version.mjs at prod build; unset in dev (-> '/godot'). */
  readonly VITE_GODOT_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
