import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

const backendTarget = process.env.VITE_BACKEND_ORIGIN ?? "http://127.0.0.1:5003";

export default defineConfig({
  plugins: [react()],
  base: "/static/react/",
  resolve: {
    alias: {
      "@/app": resolve(__dirname, "src/app"),
      "@/lib": resolve(__dirname, "src/app/lib"),
      "@/types": resolve(__dirname, "src/app/types"),
      "@/store": resolve(__dirname, "src/app/store"),
      "@/api": resolve(__dirname, "src/app/api"),
      "@/shared": resolve(__dirname, "src/shared"),
    },
  },
  build: {
    outDir: resolve(__dirname, "..", "static", "react"),
    emptyOutDir: true,
    manifest: true,
    rollupOptions: {
      input: {
        // Pipeline-events island still serves the legacy frontend at
        // ?spa=0 — kept until full legacy archive deletion. SPA-native
        // pipeline events live in views/card/PipelineEventsDrawer.tsx.
        "pipeline-events": resolve(__dirname, "src/islands/pipeline-events/main.tsx"),
        // Canonical SPA. Mounts at #root in templates/visualize.html
        // (renamed from visualize_spa.html on 2026-05-04 cutover).
        "app": resolve(__dirname, "src/app/main.tsx"),
      },
      output: {
        entryFileNames: "[name].js",
        chunkFileNames: "chunks/[name]-[hash].js",
        // Stable names for the app entry's CSS so the SPA shell can
        // reference it without reading the Vite manifest at runtime.
        // Chunks (with `[hash]`) keep their own naming above.
        assetFileNames: (assetInfo) => {
          if (assetInfo.name && assetInfo.name.endsWith(".css")) {
            return "assets/[name][extname]";
          }
          return "assets/[name]-[hash][extname]";
        },
        // Vendor split — keep ReactFlow + TanStack Query out of the main
        // app chunk so initial paint loads less code. elkjs is dynamically
        // imported in layoutEngine.ts; Rollup will keep it as its own chunk.
        manualChunks: {
          "react-vendor": ["react", "react-dom", "react-router-dom"],
          "reactflow": ["@xyflow/react"],
          "query": ["@tanstack/react-query"],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": backendTarget,
      "/static": backendTarget,
      "/visualize": backendTarget,
      "/workspace": backendTarget,
    },
  },
});
