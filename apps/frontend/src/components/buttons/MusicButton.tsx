import { useCallback, useEffect, useRef, useState } from 'react';
import volumeImg from '../../../assets/volume.svg';
import Button from './Button';

const MUSIC_URL = '/ai-town/assets/background.mp3';

export default function MusicButton() {
  const [isPlaying, setPlaying] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const getAudio = () => {
    if (!audioRef.current) {
      const audio = new Audio(MUSIC_URL);
      audio.loop = true;
      audioRef.current = audio;
    }
    return audioRef.current;
  };

  const flipSwitch = async () => {
    const audio = getAudio();
    if (isPlaying) {
      audio.pause();
    } else {
      await audio.play();
    }
    setPlaying(!isPlaying);
  };

  const handleKeyPress = useCallback(
    (event: { key: string }) => {
      if (event.key === 'm' || event.key === 'M') {
        void flipSwitch();
      }
    },
    [flipSwitch],
  );

  useEffect(() => {
    window.addEventListener('keydown', handleKeyPress);
    return () => window.removeEventListener('keydown', handleKeyPress);
  }, [handleKeyPress]);

  useEffect(() => {
    return () => {
      audioRef.current?.pause();
      audioRef.current = null;
    };
  }, []);

  return (
    <Button
      onClick={() => void flipSwitch()}
      className="hidden lg:block"
      title="Play AI generated music (press m to play/mute)"
      imgUrl={volumeImg}
    >
      {isPlaying ? 'Mute' : 'Music'}
    </Button>
  );
}
