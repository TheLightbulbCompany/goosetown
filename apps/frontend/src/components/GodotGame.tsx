/**
 * Game component that connects React state to the Godot renderer.
 *
 * Replaces the old PixiJS Game.tsx. Gets town state from TownProvider,
 * diffs the player list, and forwards spawn/move/remove calls to Godot
 * via the JS bridge.
 */

import { useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import GodotCanvas from './GodotCanvas';
import { useTownGame } from './TownProvider';
import { useGodotBridge } from '../hooks/useGodotBridge';

// Backend sends tile coordinates; Godot needs pixel coordinates
const TILE_SIZE = 32;
const toPixel = (tile: number) => tile * TILE_SIZE;

export default function GodotGame() {
  const { game } = useTownGame();
  const { ready, bridge, onEvent } = useGodotBridge();
  const [searchParams, setSearchParams] = useSearchParams();
  const hasFocused = useRef(false);
  const prevPlayerIds = useRef<Set<string>>(new Set());
  const [_selectedPlayerId, setSelectedPlayerId] = useState<string>();

  // Handle agent click events from Godot
  useEffect(() => {
    const unsub = onEvent('agent_clicked', (data) => {
      if ('id' in data) {
        setSelectedPlayerId(data.id);
      }
    });
    return unsub;
  }, [onEvent]);

  // Focus on a specific agent when ?focus=<name> is in the URL
  useEffect(() => {
    const focusName = searchParams.get('focus');
    if (!focusName || !game || !ready || hasFocused.current) return;

    let matchedPlayerId: string | undefined;
    for (const [playerId, pd] of game.playerDescriptions) {
      if (pd.name === focusName) {
        matchedPlayerId = playerId;
        break;
      }
    }
    if (!matchedPlayerId) return;

    const player = game.world.players.find((p) => p.id === matchedPlayerId);
    if (!player) return;

    hasFocused.current = true;
    setSelectedPlayerId(matchedPlayerId);
    bridge.setCamera(toPixel(player.position.x), toPixel(player.position.y), 2.0);
    setSearchParams({}, { replace: true });
  }, [searchParams, game, ready, bridge, setSearchParams]);

  // Sync player state to Godot
  useEffect(() => {
    if (!game || !ready) return;

    const currentIds = new Set<string>();

    for (const player of game.world.players) {
      currentIds.add(player.id);
      const desc = game.playerDescriptions.get(player.id);
      const spriteUrl = (desc as any)?.spriteUrl ?? '';
      const displayName = desc?.name ?? player.id;
      const facingJson = JSON.stringify(player.facing);

      if (!prevPlayerIds.current.has(player.id)) {
        // New player — spawn
        bridge.spawnAgent(
          player.id,
          spriteUrl,
          toPixel(player.position.x),
          toPixel(player.position.y),
          facingJson,
          displayName,
        );
      } else {
        // Existing player — move
        bridge.moveAgent(
          player.id,
          toPixel(player.position.x),
          toPixel(player.position.y),
          facingJson,
          player.speed * TILE_SIZE,
        );
      }

      // Update speech/activity state
      const speechBubble = game.speechBubbles.find((sb) => sb.speaker === player.id);
      if (speechBubble) {
        bridge.updateAgentState(
          player.id,
          JSON.stringify({ speechText: speechBubble.text }),
        );
      } else {
        bridge.updateAgentState(player.id, JSON.stringify({ speechText: '' }));
      }
    }

    // Remove players that are no longer present
    for (const prevId of prevPlayerIds.current) {
      if (!currentIds.has(prevId)) {
        bridge.removeAgent(prevId);
      }
    }

    prevPlayerIds.current = currentIds;
  }, [game, ready, bridge]);

  if (!game) {
    return (
      <div className="flex items-center justify-center w-full h-full text-brown-300 font-body text-lg">
        Loading town...
      </div>
    );
  }

  return (
    <>
      <div className="absolute inset-0">
        <GodotCanvas />
      </div>
      {/* Zoom controls */}
      <div className="absolute bottom-4 right-4 z-10 flex flex-col gap-1">
        <button
          className="w-8 h-8 bg-clay-700/80 hover:bg-clay-600 text-brown-100 rounded text-lg font-bold"
          onClick={() => bridge.setCamera(0, 0, 2.0)}
          title="Zoom in"
        >+</button>
        <button
          className="w-8 h-8 bg-clay-700/80 hover:bg-clay-600 text-brown-100 rounded text-lg font-bold"
          onClick={() => bridge.setCamera(0, 0, 0.8)}
          title="Zoom out"
        >-</button>
      </div>
    </>
  );
}
