import path from "node:path"
import { fileURLToPath } from "node:url"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

const root = path.dirname(fileURLToPath(import.meta.url))

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/static/ui/",
  resolve: { alias: { "@": path.resolve(root, "./src") } },
  build: {
    outDir: path.resolve(root, "../posetestbot/web/static/ui"),
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/ui": "http://127.0.0.1:5000",
      "/assets": "http://127.0.0.1:5000",
      "/calibration-targets": "http://127.0.0.1:5000",
      "/workpieces": "http://127.0.0.1:5000",
      "/pose-templates": "http://127.0.0.1:5000",
      "/bop": "http://127.0.0.1:5000",
      "/jobs": "http://127.0.0.1:5000",
      "/capture": "http://127.0.0.1:5000",
      "/preflight": "http://127.0.0.1:5000",
      "/dataset-processing": "http://127.0.0.1:5000",
      "/sensors": "http://127.0.0.1:5000",
      "/monitoring": "http://127.0.0.1:5000",
      "/run-config": "http://127.0.0.1:5000",
      "/robot": "http://127.0.0.1:5000",
      "/cluster": "http://127.0.0.1:5000",
      "/runtime": "http://127.0.0.1:5000",
      "/hardware": "http://127.0.0.1:5000",
      "/system": "http://127.0.0.1:5000"
    }
  }
})
