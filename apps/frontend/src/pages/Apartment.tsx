import { SignedIn, SignedOut } from '@clerk/clerk-react';
import LoginButton from '../components/buttons/LoginButton.tsx';

function ApartmentContent() {
  return (
    <div className="flex flex-col items-center justify-center w-full h-full gap-4">
      <p className="font-display text-xl text-brown-200 tracking-wider">Apartment</p>
      <p className="font-body text-sm text-clay-300 text-center max-w-md">
        Apartment view is being rebuilt with the new map engine.
      </p>
    </div>
  );
}

export default function Apartment() {
  return (
    <>
      <SignedIn>
        <ApartmentContent />
      </SignedIn>
      <SignedOut>
        <div className="flex flex-col items-center justify-center h-full gap-4">
          <p className="font-display text-xl text-brown-200 tracking-wider">
            Sign in to view your apartment
          </p>
          <p className="font-body text-sm text-clay-300 mb-4">
            Log in to see your agents and their activity in GooseTown.
          </p>
          <LoginButton />
        </div>
      </SignedOut>
    </>
  );
}
