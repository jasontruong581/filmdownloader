import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    // Built assets are served by the FastAPI app, so they land inside the
    // package. The directory is gitignored: it is a build artifact.
    outDir: "../src/videotrack/server/static",
    emptyOutDir: true,
  },
  server: {
    // `npm run dev` gives hot reload against the real backend.
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8756",
        changeOrigin: false,
      },
    },
  },
});
