/**
 * The subscription routes the model registry asks for their catalogues, in the
 * order it asks them. Imported once at startup.
 */

import type { ResolvedAuth } from "../auth/resolve.ts";
import { ProviderError } from "./base.ts";
import { fetchModels as fetchCodexModels } from "./codex_catalogue.ts";
import {
  CLAUDE_CODE_MODELS,
  CODEX_MODELS,
  DEVIN_MODELS,
  SUBSCRIPTIONS,
  tokenFingerprint,
} from "./models.ts";

/** A route whose catalogue client is still to come across from the Python build. */
const notPorted = (label: string) => async () => {
  throw new ProviderError(`the ${label} model list is not ported to the TypeScript build yet`);
};

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
    fetch: notPorted("Devin"),
    account: tokenFingerprint,
  },
  {
    providerId: "claude-code",
    label: "Claude Code",
    fallback: CLAUDE_CODE_MODELS,
    fetch: notPorted("Claude Code"),
    account: (auth: ResolvedAuth) => String(auth.extra.account || "") || null,
  },
);
