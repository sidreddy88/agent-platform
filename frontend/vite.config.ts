import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 4000,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
      "/incidents": { target: "http://127.0.0.1:8000" },
      "/events":    { target: "http://127.0.0.1:8000" },
      "/approvals":  { target: "http://127.0.0.1:8000" },
      "/agents":     { target: "http://127.0.0.1:8000" },
      "/monitors":   { target: "http://127.0.0.1:8000" },
      "/debug":      { target: "http://127.0.0.1:8000" },
      "/performance": { target: "http://127.0.0.1:8000" },
      "/ws/dashboard": {
        target: "ws://127.0.0.1:8000",
        ws: true,
      },
    },
  },
});
