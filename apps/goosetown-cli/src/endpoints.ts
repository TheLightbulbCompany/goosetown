/**
 * Endpoint constants for GooseTown environments.
 */

export interface Endpoints {
  apiUrl: string;
  wsUrl: string;
}

export const PROD_ENDPOINTS: Endpoints = {
  apiUrl: "https://api.goosetown.isol8.co/api/v1",
  wsUrl: "wss://ws.goosetown.isol8.co",
};

export const DEV_ENDPOINTS: Endpoints = {
  apiUrl: "https://api-dev.goosetown.isol8.co/api/v1",
  wsUrl: "wss://ws-dev.goosetown.isol8.co",
};
