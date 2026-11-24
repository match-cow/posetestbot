import path from "node:path"
import { fileURLToPath } from "node:url"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

const root = path.dirname(fileURLToPath(import.meta.url))
const apiTarget = "http://127.0.0.1:5000"
const apiPrefixes = [
  "/ui",
  "/assets",
  "/calibration",
  "/workpieces",
  "/pose-templates",
  "/bop",
  "/depth",
  "/jobs",
  "/capture",
  "/preflight",
  "/dataset-processing",
  "/sensors",
  "/monitoring",
  "/run-config",
  "/robot",
  "/cluster",
  "/runtime",
  "/hardware",
  "/system",
]

export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "/static/ui/",
  resolve: { alias: { "@": path.resolve(root, "./src") } },
  build: {
    outDir: path.resolve(root, "../posetestbot/web/static/ui"),
    emptyOutDir: true,
  },
  server: {
    proxy: Object.fromEntries(apiPrefixes.map((prefix) => [prefix, apiTarget])),
  },
})
