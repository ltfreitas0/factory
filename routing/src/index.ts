/**
 * Routing Worker for the factory control plane.
 *
 * Every request is routed to the singleton FastAPI container:
 * `getByName("singleton")` gives the container a stable identity,
 * `startAndWaitForPorts()` blocks until uvicorn is listening on 8051, and
 * `fetch()` proxies the request through (including SSE streams).
 *
 * Env vars for the FastAPI process are forwarded from this Worker's env on
 * every start: `vars` from wrangler.jsonc plus secrets from
 * `wrangler secret put`. Empty placeholders are filtered out so they never
 * reach the app as "" (e.g. FACTORY_DB="" would be treated as a path).
 */
import { Container } from "@cloudflare/containers";

export class FactoryContainer extends Container<Env> {
  /** uvicorn port (factory.api:main, FACTORY_API_PORT default 8051). */
  defaultPort = 8051;
  /** Idle sleep matches the platform's sandbox convention (DATA.md). */
  sleepAfter = "30m";
  /** The control plane talks to GitHub, model providers, and CF APIs. */
  enableInternet = true;
  requiredPorts = [8051];
}

interface Env {
  FACTORY_CONTAINER: DurableObjectNamespace<FactoryContainer>;

  // Runtime env forwarded to the FastAPI container. Placeholders in
  // wrangler.jsonc `vars`; secrets via `wrangler secret put`.
  FACTORY_DB?: string; // local sqlite path only — leave unset in D1 mode
  D1_ACCOUNT_ID?: string;
  D1_DATABASE_ID?: string;
  D1_API_TOKEN?: string;
  CLOUDFLARE_API_TOKEN?: string;
  FACTORY_FILES_BACKEND?: string; // "r2" selects the R2 backend (files.py)
  R2_ACCOUNT_ID?: string;
  R2_ACCESS_KEY_ID?: string;
  R2_SECRET_ACCESS_KEY?: string;
  R2_BUCKET?: string;
  R2_ENDPOINT?: string;
  SANDBOX_GW_URL?: string;
  SANDBOX_GW_TOKEN?: string;
  FACTORY_AUTH_TOKEN?: string;
  FACTORY_ROOT?: string;
  FACTORY_API_HOST?: string; // MUST be 0.0.0.0 in the container
  FACTORY_API_PORT?: string;
  FACTORY_CORS?: string;
  GITHUB_TOKEN?: string;
  GITHUB_OWNER?: string;
  GITHUB_OAUTH_CLIENT_ID?: string;
  GITHUB_OAUTH_CLIENT_SECRET?: string;
  GITHUB_OAUTH_REDIRECT_URI?: string;
  CLOUDFLARE_ACCOUNT_ID?: string;
  CF_OAUTH_CLIENT_ID?: string;
  CF_OAUTH_CLIENT_SECRET?: string;
  CF_OAUTH_REDIRECT_URI?: string;
}

/** Forward only non-empty string env (skips bindings, secrets, placeholders). */
function containerEnvVars(env: Env): Record<string, string> {
  return Object.fromEntries(
    Object.entries(env).filter(
      (entry): entry is [string, string] =>
        typeof entry[1] === "string" && entry[1].length > 0,
    ),
  );
}

const worker: ExportedHandler<Env> = {
  async fetch(request, env) {
    const container = env.FACTORY_CONTAINER.getByName("singleton");
    await container.startAndWaitForPorts({
      startOptions: { envVars: containerEnvVars(env) },
    });
    return container.fetch(request);
  },
};

export default worker;
