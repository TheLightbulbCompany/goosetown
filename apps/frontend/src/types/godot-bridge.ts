/** TypeScript types for the Godot ↔ React JS bridge. */

export interface GodotBridge {
  spawnAgent(id: string, spriteUrl: string, x: number, y: number, facingJson: string, name: string): void;
  moveAgent(id: string, x: number, y: number, facingJson: string, speed: number): void;
  removeAgent(id: string): void;
  updateAgentState(id: string, stateJson: string): void;
  setCamera(x: number, y: number, zoom: number): void;
}

export type GodotEventName = 'ready' | 'agent_clicked' | 'location_clicked';

export interface GodotReadyEvent {
  // empty
}

export interface GodotAgentClickedEvent {
  id: string;
}

export interface GodotLocationClickedEvent {
  name: string;
}

export type GodotEventData = GodotReadyEvent | GodotAgentClickedEvent | GodotLocationClickedEvent;

declare global {
  interface Window {
    _godotBridge?: GodotBridge;
    _onGodotEvent?: (event: string, data: GodotEventData) => void;
  }
}
