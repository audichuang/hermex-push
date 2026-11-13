import { bindings, defineConfig, exports } from "cf/config";

// Self-hosted relay for the Hermex Audi build (team 37HFR5V2JJ), deployed with `cf deploy`.
// Mirrors wrangler.jsonc; the APNs key is the team-scoped Sandbox & Production key 687ZW28C3T.
export default defineConfig({
  worker: {
    name: "hermex-relay-audi",
    entrypoint: "./src/index.ts",
    compatibilityDate: "2026-09-18",
    workersDev: true,
    previewUrls: false,
    // The URL contains a capability. Do not persist automatic request/invocation logs.
    observability: { enabled: false },
    exports: {
      InstallCoordinator: exports.durableObject({ storage: "sqlite" }),
    },
    env: {
      hermex_relay: bindings.kv({ id: "0713ddc0d5b243f58de0e07a5bb44789" }),
      INSTALLS: bindings.durableObject({ worker: "hermex-relay-audi", exportName: "InstallCoordinator" }),
      APNS_KEY_ID: bindings.text("687ZW28C3T"),
      APNS_TEAM_ID: bindings.text("37HFR5V2JJ"),
      APNS_PRIVATE_KEY: bindings.secret(),
    },
  },
});
