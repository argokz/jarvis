import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// base "./" — одна сборка работает и в Tauri, и под /app/ у ядра
export default defineConfig({
  base: "./",
  plugins: [vue()],
  clearScreen: false,
  server: { port: 5173, strictPort: true, host: process.env.TAURI_DEV_HOST || false },
  build: { target: "es2021", outDir: "dist", emptyOutDir: true },
});
