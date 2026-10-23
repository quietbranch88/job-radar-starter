import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: {
        configPath: "./wrangler.jsonc",
      },
      miniflare: {
        bindings: {
          SYNC_TOKEN: "test-only-sync-token",
        },
      },
    }),
  ],
  test: {
    include: ["../../tests/optional_sync/**/*.test.ts"],
  },
});
