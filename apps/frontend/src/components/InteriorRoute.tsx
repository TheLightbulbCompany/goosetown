import { SignedOut } from '@clerk/clerk-react';
import { Navigate } from 'react-router-dom';
import type { SceneDef } from '../scenes';

/**
 * Generic route element for any scene in the registry. When the scene
 * requires auth, signed-out users are redirected to "/". When it doesn't,
 * renders nothing (the persistent <GodotCanvas/> in App handles rendering).
 */
export default function InteriorRoute({ scene }: { scene: SceneDef }) {
  if (!scene.requireAuth) return null;
  return (
    <SignedOut>
      <Navigate to="/" replace />
    </SignedOut>
  );
}
