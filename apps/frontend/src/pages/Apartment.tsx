import { SignedOut } from '@clerk/clerk-react';
import { Navigate } from 'react-router-dom';

/**
 * Apartment page — the Godot canvas is rendered by the parent layout.
 * Redirects unauthenticated users back to the town.
 */
export default function Apartment() {
  return (
    <SignedOut>
      <Navigate to="/" replace />
    </SignedOut>
  );
}
