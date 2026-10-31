/**
 * The subscription routes the model registry asks for their catalogues, in the
 * order it asks them. Imported once at startup.
 */

import "./codex.ts";
import "./claude_code.ts";
import "./devin.ts";
import type { ResolvedAuth } from "../auth/resolve.ts";
import * as claudeCodeCatalogue from "./claude_code_catalogue.ts";
import { fetchModels as fetchCodexModels } from "./codex_catalogue.ts";
import { fetchModels as fetchDevinModels } from "./devin_catalogue.ts";
import {
  CLAUDE_CODE_MODELS,
  CODEX_MODELS,
  DEVIN_MODELS,
  SUBSCRIPTIONS,
  tokenFingerprint,
} from "./models.ts";

SUBSCRIPTIONS.push(
  {
    providerId: "openai-codex",
    label: "Codex",
    fallback: CODEX_MODELS,
    fetch: fetchCodexModels,
    account: (auth: ResolvedAuth) => String(auth.extra.account_id || "") || null,
  },
  {
    providerId: "devin",
    label: "Devin",
    fallback: DEVIN_MODELS,
    fetch: (auth: ResolvedAuth) => fetchDevinModels(auth),
    account: tokenFingerprint,
  },
  {
    providerId: "claude-code",
    label: "Claude Code",
    fallback: CLAUDE_CODE_MODELS,
    fetch: () => claudeCodeCatalogue.fetchModels(),
    account: (auth: ResolvedAuth) => String(auth.extra.account || "") || null,
    version: () => claudeCodeCatalogue.installedVersion(),
    advise: (installed: string | null) => claudeCodeCatalogue.updateAdvice(installed),
  },
);
