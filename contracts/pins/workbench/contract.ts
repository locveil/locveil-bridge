/* The Workbench plug-in contract v1 — AS CODE.
   Normative prose: docs/design/workbench.md §4 (as amended by HK-11).
   Versioned with workbench-vX. Plugins import THESE TYPES ONLY (dev dependency,
   `locveil-workbench/contract`) — never shell runtime modules. */

/** RU-first product: plain string = both locales; object = per-locale. */
export type LocalizedString = string | { ru: string; en: string };

export type Locale = "ru" | "en";

export interface PluginStatus {
  level: "ok" | "warn" | "error";
  /** Short, user-facing (e.g. connection state); shown at the plugin tab. */
  text?: LocalizedString;
}

/** Props the shell passes to every page component. */
export interface PageProps {
  locale: Locale;
  /** Deployment facts (IMPL-6): ABSOLUTE backend origins from the owner-edited shell
      config — never from build artifacts. Keys are plugin-defined; convention: "api"
      for the plugin's primary backend (e.g. http://<wb7-ip>:8080 for voice,
      :8000 for bridge). Relative fetches resolve against the SHELL origin — always
      use these. Empty when the owner configured none. */
  backends: Record<string, string>;
}

export interface PageDescriptor {
  /** Route segment under /p/<pluginId>/ — no leading slash. */
  route: string;
  title: LocalizedString;
  render: React.ComponentType<PageProps>;
}

export interface ReportContext {
  pluginId: string;
  route: string;
  locale: Locale;
}

export interface WorkbenchPlugin {
  id: string;
  title: LocalizedString;
  /** RUNTIME-registrable — may be computed (voice UI-16 schema-driven pages). */
  pages: () => PageDescriptor[];
  /** The status slot: polled by the shell (~30s + on activation). */
  status?: () => PluginStatus | Promise<PluginStatus>;
  /** The chrome BugReport button delegates here when the plugin is active. */
  reportHook?: (ctx: ReportContext) => void;
}

/* ---------- runtime assembly (HK-11) ---------- */

/** Build-emitted by the OWNING repo into its dist/ — never hand-written in commons. */
export interface ManifestFragment {
  id: string;
  version: string;
  /** ESM entry, relative to the fragment. Default export: WorkbenchPlugin. */
  entry: string;
  /** Stylesheets the shell injects/removes. */
  styles?: string[];
  /** Strict-major match against the shell singletons; mismatch = refuse-and-surface. */
  peers: Record<string, string>;
  /** Optional per-plugin backend compatibility statement (informational v1). */
  backendCompat?: Record<string, string>;
}

/** Conjunction of gate references — ALL must hold before the slot activates. */
export interface GateDescriptor {
  all: string[];
}

/** One entry of the owner-edited shell config (workbench.config.json). */
export type ConfigEntry =
  | { location: string; backends?: Record<string, string> } // dev-phase: sibling dist path; later: URL
  | { id: string; title: LocalizedString; gate: GateDescriptor }; // dormant: ZERO activity

export interface WorkbenchConfig {
  plugins: ConfigEntry[];
}

import type * as React from "react";
