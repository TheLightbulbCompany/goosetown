import { useNavigate } from 'react-router-dom';

export default function LoginButton() {
  const navigate = useNavigate();

  return (
    <button
      onClick={() => navigate('/sign-in')}
      className="button text-white shadow-solid"
    >
      <div className="inline-block bg-clay-700">
        <span>Log in</span>
      </div>
    </button>
  );
}
