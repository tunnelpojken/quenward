import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

export default defineConfig({
  plugins: [svelte()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // No inline scripts or source maps in production: the CSP only allows our own files.
    sourcemap: false,
    assetsInlineLimit: 0,
  },
  server: {
    // `npm run dev` on :5173 forwards backend calls to the app on :5005.
    port: 5173,
    proxy: {
      "/healthz": "http://127.0.0.1:5005",
      "/api": "http://127.0.0.1:5005",
      "/ws": { target: "ws://127.0.0.1:5005", ws: true },
    },
  },
});
