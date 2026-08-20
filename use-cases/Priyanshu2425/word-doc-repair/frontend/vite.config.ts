import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { viteSingleFile } from "vite-plugin-singlefile";

/** One file into the directory the FastAPI app already reads. No install, no
 *  build step, no Node on the machine that serves it. */
export default defineConfig({
  plugins: [react(), viteSingleFile()],
  build: { outDir: "../static", emptyOutDir: true, target: "es2020", cssMinify: true },
});
