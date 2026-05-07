import { defineConfig } from "vitest/config";
import { resolve } from "node:path";

export default defineConfig({
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
  test: {
    environment: "node",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
    reporters: "default",
  },
});
