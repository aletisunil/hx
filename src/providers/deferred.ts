/**
 * A route that was not signed in when HX started.
 *
 * The TUI opens anyway, so `/login` is one command away rather than a prompt
 * in the shell before anything is on screen. The real provider is built on the
 * first turn after a sign-in - whichever way it happened: `/login`,
 * `/configure`, `hx auth` in another terminal. Until then every turn says how
 * to sign in.
 */

import { type AuthResolver, ExpiredCredential, MissingCredential } from "../auth/resolve.ts";
import { type Provider, ProviderError, type ProviderRequest, type StreamItem } from "./base.ts";
import type { ModelRegistry } from "./models.ts";
import { available, buildProvider, providerFor } from "./registry.ts";

/** What the TUI says when no route at all is signed in. */
export const NOT_SIGNED_IN =
  "Not signed in yet. Type /login to sign in with ChatGPT Plus/Pro, Claude Pro/Max, an OpenRouter API key or Devin.";

/** What each route needs, as a sentence names it. */
const NEEDS: Record<string, string> = {
  openrouter: "an OpenRouter API key",
  "openai-codex": "a ChatGPT Plus/Pro sign-in",
  "claude-code": "a Claude Pro/Max sign-in",
  devin: "a Devin sign-in",
};

/** What to tell the user about `modelId` while its route has no credential:
 * how to sign in, and - when another route is signed in - that `/model` can
 * pick one of its models instead. */
export function signInHint(modelId: string, resolver: AuthResolver): string {
  if (!available(resolver).length) return NOT_SIGNED_IN;
  const route = providerFor(modelId);
  const needs = NEEDS[route.id] ?? `a ${route.label} sign-in`;
  return `${modelId} needs ${needs}, which is not set up yet. Type /login to sign in, or /model to pick a model you can use.`;
}

export class DeferredProvider implements Provider {
  private inner: Provider | null = null;

  constructor(
    readonly modelId: string,
    private readonly resolver: AuthResolver,
    private sessionId: string | null,
    private readonly models: ModelRegistry | null,
  ) {}

  get name(): string {
    return this.inner?.name ?? providerFor(this.modelId).id;
  }

  /** The prompt-cache key, kept for the provider this becomes. */
  setSessionId(sessionId: string): void {
    this.sessionId = sessionId;
    (this.inner as { setSessionId?: (id: string) => void } | null)?.setSessionId?.(sessionId);
  }

  /** A key saved now is read from the store when the provider is built. */
  setApiKey(key: string): void {
    (this.inner as { setApiKey?: (key: string) => void } | null)?.setApiKey?.(key);
  }

  async *astream(request: ProviderRequest, signal: AbortSignal): AsyncGenerator<StreamItem> {
    if (this.inner === null) {
      try {
        this.inner = buildProvider(request.model, this.resolver, {
          sessionId: this.sessionId,
          models: this.models,
        });
      } catch (error) {
        if (error instanceof MissingCredential || error instanceof ExpiredCredential)
          throw new ProviderError(signInHint(request.model, this.resolver));
        throw error;
      }
    }
    yield* this.inner.astream(request, signal);
  }

  async aclose(): Promise<void> {
    await this.inner?.aclose();
  }
}
