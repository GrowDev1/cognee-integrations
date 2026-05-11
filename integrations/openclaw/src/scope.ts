import type { CogneePluginConfig, MemoryScope, ScopeRoute } from "./types.js";

// ---------------------------------------------------------------------------
// Glob matching — uses picomatch-style algorithm (*, **, ?)
//
// Fix #3: Proper glob support instead of naive regex. Handles:
//   *  — any non-separator chars in one segment
//   ** — any depth of segments
//   ?  — single non-separator char
//   [abc] — character class
// ---------------------------------------------------------------------------

/**
 * Convert a glob pattern to a RegExp. Supports *, **, ?, and [charclass].
 */
function globToRegex(pattern: string): RegExp {
  let i = 0;
  let regex = "^";

  while (i < pattern.length) {
    const c = pattern[i];

    if (c === "*") {
      if (pattern[i + 1] === "*") {
        // ** — match any depth
        // Consume optional trailing slash
        if (pattern[i + 2] === "/") {
          regex += "(?:.+/)?";
          i += 3;
        } else {
          regex += ".*";
          i += 2;
        }
      } else {
        // * — match within one segment
        regex += "[^/]*";
        i++;
      }
    } else if (c === "?") {
      regex += "[^/]";
      i++;
    } else if (c === "[") {
      // Character class — pass through until ]
      const start = i;
      i++; // skip [
      if (i < pattern.length && pattern[i] === "!") {
        regex += "[^";
        i++;
      } else {
        regex += "[";
      }
      while (i < pattern.length && pattern[i] !== "]") {
        regex += escapeRegexChar(pattern[i]);
        i++;
      }
      regex += "]";
      i++; // skip ]
    } else if (".+^${}()|\\".includes(c)) {
      regex += "\\" + c;
      i++;
    } else {
      regex += c;
      i++;
    }
  }

  regex += "$";
  return new RegExp(regex);
}

function escapeRegexChar(c: string): string {
  if ("-]\\^".includes(c)) return "\\" + c;
  return c;
}

/**
 * Match a file path against a glob pattern.
 * Paths are normalized to forward slashes.
 */
export function matchGlob(pattern: string, filePath: string): boolean {
  const normalizedPath = filePath.replace(/\\/g, "/");
  const normalizedPattern = pattern.replace(/\\/g, "/");
  return globToRegex(normalizedPattern).test(normalizedPath);
}

/**
 * Route a file to its memory scope based on scopeRouting rules.
 * First matching rule wins; falls back to defaultScope.
 */
export function routeFileToScope(
  filePath: string,
  routes: ScopeRoute[],
  defaultScope: MemoryScope,
): MemoryScope {
  const normalized = filePath.replace(/\\/g, "/");
  for (const route of routes) {
    if (matchGlob(route.pattern, normalized)) {
      return route.scope;
    }
  }
  return defaultScope;
}

/**
 * Determine whether multi-scope mode is active.
 * Active when at least one scope-specific dataset prefix/name/template is configured.
 */
export function isMultiScopeEnabled(cfg: Required<CogneePluginConfig>): boolean {
  return !!(cfg.companyDataset || cfg.userDatasetPrefix || cfg.agentDatasetPrefix || cfg.agentDatasetTemplate);
}

/**
 * Resolve the Cognee dataset name for a given memory scope.
 *
 * `runtimeAgentId` lets multi-agent gateways pass the active agent's identity
 * (from PluginHookAgentContext.agentId) so the "agent" scope routes to the
 * caller's dataset rather than the static plugin-config agentId. Falls back to
 * cfg.agentId when runtimeAgentId is absent (CLI / background sync paths).
 */
export function datasetNameForScope(
  scope: MemoryScope,
  cfg: Required<CogneePluginConfig>,
  runtimeAgentId?: string,
): string {
  switch (scope) {
    case "company":
      return cfg.companyDataset || `${cfg.datasetName}-company`;
    case "user":
      return cfg.userDatasetPrefix
        ? `${cfg.userDatasetPrefix}-${cfg.userId || "default"}`
        : `${cfg.datasetName}-user-${cfg.userId || "default"}`;
    case "agent": {
      const id = runtimeAgentId?.trim() || cfg.agentId || "default";
      if (cfg.agentDatasetTemplate) {
        return cfg.agentDatasetTemplate.replace(/\{agentId\}/g, id);
      }
      return cfg.agentDatasetPrefix
        ? `${cfg.agentDatasetPrefix}-${id}`
        : `${cfg.datasetName}-agent-${id}`;
    }
  }
}
