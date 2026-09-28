import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// API_TARGET: point the dev proxy elsewhere when port 8000 is taken locally.
const API = process.env.API_TARGET ?? "http://127.0.0.1:8000";
const WS = API.replace(/^http/, "ws");

export default defineConfig({
  plugins: [react()],
  server: {
    port: 4000,
    proxy: {
      "/api": {
        target: API,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
      "/incidents": { target: API },
      "/events":    { target: API },
      "/approvals":  { target: API },
      "/agents":     { target: API },
      "/monitors":   { target: API },
      "/metrics":    { target: API },
      "/debug":      { target: API },
      "/performance": { target: API },
      "/ws/dashboard": {
        target: WS,
        ws: true,
      },
    },
  },
});
