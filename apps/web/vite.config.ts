import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built UI is served by the Python package (gpudock_core/web/dist).
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../../packages/core/src/gpudock_core/web/dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    // `gpudock --dev` runs the API on 8765; changeOrigin keeps its Host check happy.
    proxy: { "/api": { target: "http://127.0.0.1:8765", changeOrigin: true } },
  },
});
