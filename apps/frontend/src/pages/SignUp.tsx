import { SignUp } from '@clerk/clerk-react';

export default function SignUpPage() {
  return (
    <div className="flex items-center justify-center min-h-[100dvh] bg-clay-900">
      <SignUp routing="path" path="/sign-up" signInUrl="/sign-in" />
    </div>
  );
}
