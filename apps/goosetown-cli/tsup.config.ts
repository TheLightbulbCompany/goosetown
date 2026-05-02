import { defineConfig } from "tsup";

export default defineConfig({
  entry: ["src/cli.ts"],
  format: ["cjs"],
  outDir: "dist",
  splitting: false,
  sourcemap: false,
  clean: true,
  minify: false,
  target: "node18",
  noExternal: [],
  shims: false,
});
