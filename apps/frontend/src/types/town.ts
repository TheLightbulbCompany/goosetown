/**
 * TypeScript types matching the FastAPI backend /town/* API responses.
 * Replaces the Convex type classes (World, Player, WorldMap, etc.).
 */

export interface TownPlayer {
  id: string; // "p:0", "p:1", ...
  position: { x: number; y: number };
  facing: { dx: number; dy: number };
  speed: number;
  lastInput: number;
}

export interface TownAgent {
  id: string; // "a:0", "a:1", ...
  playerId: string;
  context?: string; // Omitted by the legacy town-only REST response.
}

export interface TownWorld {
  nextId: number;
  players: TownPlayer[];
  agents: TownAgent[];
  // Legacy field from the AI Town schema. The backend always returns
  // ``[]`` here; active conversations live at ``GET /town/conversations``
  // which has its own ``ConversationTurn`` / ``TownConversationResponse``
  // types. Kept as ``unknown[]`` rather than ``any[]`` so accidental
  // consumers are forced to type-narrow instead of silently accepting
  // anything.
  conversations: unknown[];
}

export interface TownEngine {
  currentTime: number;
  lastStepTs: number;
}

export interface PlayerDescription {
  playerId: string;
  name: string;
  description: string;
  character: string; // "c1" .. "c12"
}

export interface AgentDescription {
  agentId: string;
  identity: string;
  plan: string;
}

export interface SpeechBubble {
  speaker: string;
  text: string;
  conversation_id: string;
}

export interface WorldMap {
  width: number;
  height: number;
  tileDim: number;
  tileSetUrl: string;
  mapUrl: string;
}

/** Response from GET /town/state */
export interface TownStateResponse {
  world: TownWorld;
  engine: TownEngine;
  speechBubbles: SpeechBubble[];
}

/** Response from GET /town/descriptions */
export interface TownDescriptionsResponse {
  worldMap: WorldMap;
  playerDescriptions: PlayerDescription[];
  agentDescriptions: AgentDescription[];
}

/** Combined game state used by rendering components */
export interface TownGameState {
  world: TownWorld;
  engine: TownEngine;
  worldMap: WorldMap;
  playerDescriptions: Map<string, PlayerDescription>;
  agentDescriptions: Map<string, AgentDescription>;
  speechBubbles: SpeechBubble[];
}

/** Response from GET /town/status */
export interface TownStatusResponse {
  worldId: string;
  engineId: string;
  status: string;
  lastViewed: number;
  isDefault: boolean;
}

/** WS push message for town_state (legacy backend) */
export interface TownWsMessage {
  type: 'town_state';
  worldState: { world: TownWorld; engine: TownEngine };
  gameDescriptions: TownDescriptionsResponse;
  speechBubbles: SpeechBubble[];
}

/** WS push message from game server (world_state event) */
export interface GameServerWorldState {
  event: 'world_state';
  agents: Array<{
    id: string;
    name: string;
    x: number;
    y: number;
    facing: string;
    is_moving: boolean;
    location: string;
    context?: string;
    spriteUrl: string;
  }>;
}
