import { SignIn } from '@clerk/clerk-react';

export default function SignInPage() {
  return (
    <div className="flex items-center justify-center min-h-[100dvh] bg-clay-900">
      <SignIn routing="path" path="/sign-in" signUpUrl="/sign-up" />
    </div>
  );
}
