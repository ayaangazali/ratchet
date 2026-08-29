import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: proxy the gateway so the UI talks to it same-origin (no CORS, SSE passes through).
// Build: emit static assets the gateway serves from apps/web/dist.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8080", changeOrigin: true },
      "/healthz": { target: "http://localhost:8080", changeOrigin: true },
      "/eval": { target: "http://localhost:8080", changeOrigin: true },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
