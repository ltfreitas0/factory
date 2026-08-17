/**
 * sandbox-gw — Cloudflare Sandbox gateway Worker.
 *
 * Thin, authenticated HTTP gateway over the Cloudflare Sandbox SDK (stable
 * `@cloudflare/sandbox` line). The factory FastAPI backend talks to this
 * Worker to drive one ephemeral container per project.
 *
 * Layout contract (see .meta/DATA.md):
 *   - one sandbox per project: `getSandbox(env.Sandbox, <project>, { normalizeId: true })`
 *   - `sleepAfter "30m"`; `destroy()` when a project is deleted
 *   - `/workspace/{project}/` mirrors `repo/{owner}/{project}/state/`
 *
 * Every request is first offered to `proxyToSandbox()` so preview URLs
 * (`https://<port>-<sandbox>-<token>.<worker-host>`) reach the container;
 * everything else is the JSON gateway API below, guarded by a shared bearer
 * token (`GATEWAY_TOKEN` secret).
 */
import {
  getSandbox,
  proxyToSandbox,
  Sandbox as BaseSandbox,
  ContainerProxy,
  ContainerUnavailableError,
  OperationInterruptedError,
  isPlatformTransientError,
} from "@cloudflare/sandbox";

/**
 * The Sandbox DO class. Subclassed (not the bare SDK export) so outbound
 * internet is explicitly enabled — the stable SDK can default it off, which
 * kills package installs and model calls inside the container. See the
 * outbound-traffic guide: `enableInternet = true` must be set for egress.
 */
export class Sandbox extends BaseSandbox {
  enableInternet = true;
}

/** The Sandbox Durable Object class must be exported for the `containers` binding. */
/**
 * ContainerProxy must be exported (and registered as a DO) for outbound
 * traffic to work — without it sandbox egress (package installs, model
 * calls) is dead. See the Sandbox outbound-traffic guide.
 */
export { ContainerProxy };

export interface Env {
  /** Sandbox Durable Object binding (wrangler.jsonc `durable_objects` + `containers`). */
  Sandbox: DurableObjectNamespace<Sandbox>;
  /** Gateway bearer token — `wrangler secret put GATEWAY_TOKEN` (local: `.dev.vars`). */
  GATEWAY_TOKEN: string;
}

const WORKSPACE_ROOT = "/workspace";
const DEFAULT_EXEC_TIMEOUT_S = 300;
const MAX_EXEC_TIMEOUT_S = 3600;
/** Auto-sleep after this much idle time (ratified in .meta/DATA.md). */
const SLEEP_AFTER = "30m";
/** Container-startup retry policy: 3 tries, 2s backoff. */
const ENSURE_TRIES = 3;
const ENSURE_BACKOFF_MS = 2_000;
/** Slug form shared with the FastAPI backend (normalize(project.slug)). */
const PROJECT_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

/** Request body accepted by the JSON routes. Fields are validated per-route. */
interface GatewayBody {
  project?: unknown;
  command?: unknown;
  timeout?: unknown;
  content?: unknown;
  encoding?: unknown;
  port?: unknown;
  hostname?: unknown;
}

class HttpError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly code?: string,
  ) {
    super(message);
  }
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/**
 * Constant-time string comparison: hash both sides with SHA-256 and XOR the
 * equal-length digests (Workers has no crypto.timingSafeEqual).
 */
async function timingSafeEqual(a: string, b: string): Promise<boolean> {
  const encoder = new TextEncoder();
  const [left, right] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(a)),
    crypto.subtle.digest("SHA-256", encoder.encode(b)),
  ]);
  const la = new Uint8Array(left);
  const lb = new Uint8Array(right);
  let diff = 0;
  for (let i = 0; i < la.length; i++) diff |= la[i] ^ lb[i];
  return diff === 0;
}

async function isAuthorized(request: Request, expected: string): Promise<boolean> {
  if (!expected) return false;
  const header = request.headers.get("authorization") ?? "";
  const match = /^Bearer\s+(.+)$/i.exec(header);
  if (!match) return false;
  return timingSafeEqual(match[1], expected);
}

function isValidProject(project: unknown): project is string {
  return (
    typeof project === "string" &&
    project.length > 0 &&
    project !== "." &&
    project !== ".." &&
    PROJECT_RE.test(project)
  );
}

function requireProject(body: GatewayBody): string {
  if (!isValidProject(body.project)) {
    throw new HttpError(400, "project must be a valid slug ([A-Za-z0-9._-], no '..')");
  }
  return body.project;
}

/**
 * Resolve a caller-supplied relative path under /workspace/{project}/.
 * Rejects absolute paths, backslashes, and `..` segments (path traversal).
 * Empty segments are normalized away (`a//b` → `a/b`).
 */
function resolveWorkspacePath(project: string, rel: string): string | null {
  if (!rel || rel.startsWith("/") || rel.includes("\\")) return null;
  const segments = rel.split("/");
  if (segments.some((segment) => segment === "..")) return null;
  return `${WORKSPACE_ROOT}/${project}/${segments.filter(Boolean).join("/")}`;
}

function dirname(path: string): string {
  const index = path.lastIndexOf("/");
  return index <= 0 ? "/" : path.slice(0, index);
}

async function readBody(request: Request): Promise<GatewayBody> {
  try {
    return (await request.json()) as GatewayBody;
  } catch {
    throw new HttpError(400, "invalid JSON body");
  }
}

/**
 * The stable SDK surfaces "container still booting" as a
 * `ContainerUnavailableError` (code `CONTAINER_UNAVAILABLE`, reason
 * `container_starting`) or an `OperationInterruptedError` with a retryable
 * context. Both are transient — retry the same operation.
 */
function isRetryableContainerStartup(err: unknown): boolean {
  if (err instanceof ContainerUnavailableError) return true;
  if (err instanceof OperationInterruptedError) {
    return err.context.retryable !== false;
  }
  return isPlatformTransientError(err);
}

/** Run `fn` with ENSURE_TRIES attempts and ENSURE_BACKOFF_MS between retries. */
async function withContainerRetry<T>(fn: () => Promise<T>): Promise<T> {
  for (let attempt = 1; ; attempt++) {
    try {
      return await fn();
    } catch (err) {
      if (!isRetryableContainerStartup(err) || attempt >= ENSURE_TRIES) throw err;
      await sleep(ENSURE_BACKOFF_MS);
    }
  }
}

function sandboxFor(env: Env, project: string): Sandbox {
  return getSandbox(env.Sandbox, project, {
    normalizeId: true,
    sleepAfter: SLEEP_AFTER,
    // Sessionless: every exec runs in a fresh process, so `exit`/shell
    // crashes can never poison a persistent default session.
    enableDefaultSession: false,
  });
}

/**
 * Get the project sandbox and make sure its container is up (forces the lazy
 * container start, retrying while the platform provisions it) and the
 * /workspace/{project}/ directory exists.
 */
async function ensureSandbox(env: Env, project: string): Promise<Sandbox> {
  const sandbox = sandboxFor(env, project);
  const workspace = `${WORKSPACE_ROOT}/${project}`;
  await withContainerRetry(async () => {
    await sandbox.mkdir(workspace, { recursive: true });
  });
  return sandbox;
}

async function handleEnsure(env: Env, body: GatewayBody): Promise<Response> {
  const project = requireProject(body);
  await ensureSandbox(env, project);
  // normalizeId: true lowercases the DO id; the sandbox_id IS that key.
  return json({ ok: true, sandbox_id: project.toLowerCase() });
}

/** Matches errors where the sandbox shell session died and cannot run more commands. */
const DEAD_SESSION_RE = /session.*(?:died|not ready|terminated)/i;

function isDeadSessionError(err: unknown): boolean {
  const info = sdkErrorInfo(err);
  if (info?.code === "SESSION_TERMINATED") return true;
  if (info?.code !== "COMMAND_EXECUTION_ERROR") return false;
  return DEAD_SESSION_RE.test(err instanceof Error ? err.message : String(err));
}

/** The container embeds the shell exit code in the dead-session message. */
function deadSessionExitCode(err: unknown): number | undefined {
  const match = /\(exit code: (\d+)\)/i.exec(err instanceof Error ? err.message : String(err));
  return match ? Number(match[1]) : undefined;
}

async function handleExec(env: Env, body: GatewayBody): Promise<Response> {
  const project = requireProject(body);
  if (typeof body.command !== "string" || body.command.length === 0) {
    throw new HttpError(400, "command must be a non-empty string");
  }
  const timeoutS = body.timeout === undefined ? DEFAULT_EXEC_TIMEOUT_S : Number(body.timeout);
  if (!Number.isFinite(timeoutS) || timeoutS <= 0 || timeoutS > MAX_EXEC_TIMEOUT_S) {
    throw new HttpError(400, `timeout must be seconds in (0, ${MAX_EXEC_TIMEOUT_S}]`);
  }
  const sandbox = await ensureSandbox(env, project);
  const execOptions = {
    timeout: Math.round(timeoutS * 1000), // ExecOptions.timeout is milliseconds
    cwd: `${WORKSPACE_ROOT}/${project}`,
  };
  try {
    const result = await sandbox.exec(body.command, execOptions);
    return json({
      ok: result.success,
      stdout: result.stdout,
      stderr: result.stderr,
      exitCode: result.exitCode,
    });
  } catch (err) {
    if (isDeadSessionError(err)) {
      // The command killed its own shell (e.g. `exit N`); the exec session is
      // gone for good. Reset the sandbox so the next call gets a fresh
      // session, and report the outcome in the normal exec shape. The
      // container filesystem is ephemeral by contract — clients re-sync from
      // R2 state.
      await sandbox.destroy().catch(() => undefined);
      const exitCode = deadSessionExitCode(err);
      const stderr = err instanceof Error ? err.message : String(err);
      return json({ ok: exitCode === 0, stdout: "", stderr, exitCode });
    }
    throw err;
  }
}

async function handlePutFile(env: Env, project: string, rel: string, body: GatewayBody): Promise<Response> {
  const full = resolveWorkspacePath(project, rel);
  if (!full) throw new HttpError(400, "invalid file path");
  if (typeof body.content !== "string") {
    throw new HttpError(400, "content must be a string (base64 when encoding is 'base64')");
  }
  const encoding = body.encoding === undefined ? "utf-8" : body.encoding;
  if (encoding !== "utf-8" && encoding !== "utf8" && encoding !== "base64") {
    throw new HttpError(400, "encoding must be 'utf-8' or 'base64'");
  }
  const sandbox = await ensureSandbox(env, project);
  // Parent dirs may not exist yet; writeFile will surface any real error.
  await sandbox.mkdir(dirname(full), { recursive: true }).catch(() => undefined);
  await sandbox.writeFile(full, body.content, encoding === "base64" ? { encoding: "base64" } : undefined);
  return json({ ok: true });
}

async function handleGetFile(env: Env, project: string, rel: string): Promise<Response> {
  const full = resolveWorkspacePath(project, rel);
  if (!full) throw new HttpError(400, "invalid file path");
  const sandbox = await ensureSandbox(env, project);
  const result = await sandbox.readFile(full);
  // The SDK auto-detects binary files and returns their content base64-encoded.
  return json({ ok: true, content: result.content, encoding: result.encoding ?? "utf-8" });
}

async function handleExposePort(env: Env, request: Request, body: GatewayBody): Promise<Response> {
  const project = requireProject(body);
  const port = Number(body.port);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) {
    throw new HttpError(400, "port must be an integer in [1024, 65535]");
  }
  // The SDK needs the gateway Worker's hostname to build the preview URL;
  // default to the host this request came in on. Under wrangler dev the SDK
  // maps it to *.localhost itself.
  const hostname =
    typeof body.hostname === "string" && body.hostname.length > 0
      ? body.hostname
      : new URL(request.url).hostname;
  const sandbox = await ensureSandbox(env, project);
  const exposed = await sandbox.exposePort(port, { hostname });
  return json({ ok: true, url: exposed.url, port: exposed.port });
}

async function handleSync(env: Env, body: GatewayBody): Promise<Response> {
  const project = requireProject(body);
  const sandbox = await ensureSandbox(env, project);
  return json({ ok: true, workspace: `${WORKSPACE_ROOT}/${project}` });
}

async function handleSleep(env: Env, body: GatewayBody): Promise<Response> {
  const project = requireProject(body);
  // The stable SDK has no imperative sleep() API: containers auto-sleep via
  // the `sleepAfter` option passed to getSandbox (30m for this gateway).
  return json({ ok: true, note: "sleep via options" });
}

async function handleDestroy(env: Env, body: GatewayBody): Promise<Response> {
  const project = requireProject(body);
  const sandbox = sandboxFor(env, project);
  await sandbox.destroy();
  return json({ ok: true });
}

/**
 * Structural check for SDK errors. The base `SandboxError` class is declared
 * but not exported by @cloudflare/sandbox 0.12.7, and DO RPC clones errors as
 * plain objects that keep only own properties — the SDK's `errorResponse`
 * payload is one of them, so read `code`/`httpStatus` from there.
 */
interface SdkErrorShape {
  code?: unknown;
  httpStatus?: unknown;
  errorResponse?: { code?: unknown; httpStatus?: unknown; message?: unknown };
}

function sdkErrorInfo(err: unknown): { code?: string; status?: number } | undefined {
  if (typeof err !== "object" || err === null) return undefined;
  const shape = err as SdkErrorShape;
  const er = shape.errorResponse;
  const code =
    typeof er?.code === "string" ? er.code : typeof shape.code === "string" ? shape.code : undefined;
  const rawStatus = er?.httpStatus ?? shape.httpStatus;
  const status =
    typeof rawStatus === "number" && rawStatus >= 400 && rawStatus < 600 ? rawStatus : undefined;
  return { code, status };
}

function toErrorResponse(err: unknown): Response {
  if (err instanceof HttpError) {
    return json({ ok: false, error: err.message, code: err.code }, err.status);
  }
  const info = sdkErrorInfo(err);
  const status = info?.code === "FILE_NOT_FOUND" ? 404 : (info?.status ?? 500);
  const message = err instanceof Error ? err.message : String(err);
  return json({ ok: false, error: message, code: info?.code }, status);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    // (a) Preview URL proxying — MUST run first so dev preview URLs work.
    const proxied = await proxyToSandbox(request, env);
    if (proxied) return proxied;

    // (b) Gateway API — bearer token required.
    if (!(await isAuthorized(request, env.GATEWAY_TOKEN))) {
      return json({ ok: false, error: "unauthorized" }, 401);
    }

    const url = new URL(request.url);
    const filesMatch = /^\/files\/([^/]+)\/(.+)$/.exec(url.pathname);

    try {
      if (request.method === "POST" && url.pathname === "/ensure") {
        return await handleEnsure(env, await readBody(request));
      }
      if (request.method === "POST" && url.pathname === "/exec") {
        return await handleExec(env, await readBody(request));
      }
      if (filesMatch) {
        const project = decodeURIComponent(filesMatch[1]);
        const rel = decodeURIComponent(filesMatch[2]);
        if (!isValidProject(project)) {
          throw new HttpError(400, "invalid project in path");
        }
        if (request.method === "PUT") {
          return await handlePutFile(env, project, rel, await readBody(request));
        }
        if (request.method === "GET") {
          return await handleGetFile(env, project, rel);
        }
      }
      if (request.method === "POST" && url.pathname === "/ports/expose") {
        return await handleExposePort(env, request, await readBody(request));
      }
      if (request.method === "POST" && url.pathname === "/sync") {
        return await handleSync(env, await readBody(request));
      }
      if (request.method === "POST" && url.pathname === "/sleep") {
        return await handleSleep(env, await readBody(request));
      }
      if (request.method === "POST" && url.pathname === "/destroy") {
        return await handleDestroy(env, await readBody(request));
      }
      return json({ ok: false, error: "not found" }, 404);
    } catch (err) {
      return toErrorResponse(err);
    }
  },
};
