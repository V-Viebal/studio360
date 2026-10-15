const ACCESS_TOKEN_KEY = "studio360.auth.access-token";
const REFRESH_TOKEN_KEY = "studio360.auth.refresh-token";
const STATE_KEY = "studio360.auth.oidc-state";
const VERIFIER_KEY = "studio360.auth.oidc-verifier";
const LEGACY_TOKEN_KEY = "arcreel_auth_token";

export interface LogtoConfig {
  issuer: string;
  clientId: string;
  resource: string;
  redirectUri: string;
}

function viteEnv(): Record<string, string | undefined> {
  return (import.meta as ImportMeta & {
    env?: Record<string, string | undefined>;
  }).env ?? {};
}

export function getLogtoConfig(): LogtoConfig {
  const env = viteEnv();
  const issuer = String(env["VITE_STUDIO360_LOGTO_ISSUER"] ?? "").trim().replace(/\/+$/, "");
  const clientId = String(env["VITE_STUDIO360_LOGTO_CLIENT_ID"] ?? "").trim();
  const resource = String(env["VITE_STUDIO360_LOGTO_API_RESOURCE"] ?? "").trim().replace(/\/+$/, "");
  const redirectUri = typeof window === "undefined"
    ? ""
    : `${window.location.origin}/login`;
  return { issuer, clientId, resource, redirectUri };
}

export function isLogtoConfigured(): boolean {
  const config = getLogtoConfig();
  return Boolean(
    config.issuer
    && config.clientId
    && config.resource
    && isAllowedLogtoUrl(config.issuer)
    && isAllowedLogtoUrl(config.resource),
  );
}

function isAllowedLogtoUrl(value: string): boolean {
  try {
    const parsed = new URL(value);
    const isLocal = ["localhost", "127.0.0.1", "::1"].includes(parsed.hostname);
    return parsed.protocol === "https:" || (parsed.protocol === "http:" && isLocal);
  } catch {
    return false;
  }
}

export function isLogtoPartiallyConfigured(): boolean {
  const env = viteEnv();
  return [
    "VITE_STUDIO360_LOGTO_ISSUER",
    "VITE_STUDIO360_LOGTO_CLIENT_ID",
    "VITE_STUDIO360_LOGTO_API_RESOURCE",
  ].some(key => Boolean(String(env[key] ?? "").trim())) && !isLogtoConfigured();
}

function session(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return typeof window !== "undefined" ? window.sessionStorage : null;
  } catch {
    return null;
  }
}

function readSession(key: string): string | null {
  try {
    return session()?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function writeSession(key: string, value: string): void {
  try {
    session()?.setItem(key, value);
  } catch {
    // A blocked storage mode will fail the callback state check closed.
  }
}

function removeSession(key: string): void {
  try {
    session()?.removeItem(key);
  } catch {
    // Best-effort cleanup.
  }
}

function removeLegacyToken(): void {
  try {
    if (typeof window !== "undefined") window.localStorage.removeItem(LEGACY_TOKEN_KEY);
  } catch {
    // Privacy modes can deny localStorage; sessionStorage remains isolated.
  }
}

export function getToken(): string | null {
  removeLegacyToken();
  return readSession(ACCESS_TOKEN_KEY);
}

export function setToken(token: string): void {
  removeLegacyToken();
  writeSession(ACCESS_TOKEN_KEY, token);
}

export function getRefreshToken(): string | null {
  return readSession(REFRESH_TOKEN_KEY);
}

function setRefreshToken(token: string): void {
  writeSession(REFRESH_TOKEN_KEY, token);
}

export function clearToken(): void {
  [ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY, STATE_KEY, VERIFIER_KEY].forEach(removeSession);
  removeLegacyToken();
}

export function getAuthHeader(): string | null {
  const token = getToken();
  return token ? `Bearer ${token}` : null;
}

export function withAuthQuery(url: string): string {
  const token = getToken();
  if (!token) return url;
  try {
    const candidate = new URL(url, window.location.origin);
    if (candidate.origin !== window.location.origin || !candidate.pathname.startsWith("/api/v1/")) {
      return url;
    }
  } catch {
    return url;
  }
  const separator = url.includes("?") ? "&" : "?";
  return `${url}${separator}token=${encodeURIComponent(token)}`;
}

function decodeToken(token: string | null): Record<string, unknown> | null {
  if (!token) return null;
  try {
    const segment = token.split(".")[1];
    if (!segment) return null;
    const normalized = segment.replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(normalized.padEnd(normalized.length + ((4 - normalized.length % 4) % 4), "="))) as Record<string, unknown>;
  } catch {
    return null;
  }
}

function claimValues(value: unknown): string[] {
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (trimmed.startsWith("[")) {
      try {
        const parsed: unknown = JSON.parse(trimmed);
        if (Array.isArray(parsed)) return claimValues(parsed);
      } catch {
        return [];
      }
    }
    return trimmed.replace(/[,;]/g, " ").split(/\s+/).filter(Boolean);
  }
  if (Array.isArray(value)) return value.flatMap(claimValues);
  return [];
}

function firstStringClaim(claims: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = claims[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return "";
}

export function getTokenClaims(token = getToken()): Record<string, unknown> | null {
  return decodeToken(token);
}

export function isValidAccessToken(token = getToken()): boolean {
  const config = getLogtoConfig();
  const claims = decodeToken(token);
  if (!claims || !isLogtoConfigured()) return false;
  if (claims["iss"] !== config.issuer) return false;
  const audiences = Array.isArray(claims["aud"])
    ? claims["aud"].map(String)
    : typeof claims["aud"] === "string" ? [claims["aud"]] : [];
  if (!audiences.includes(config.resource)) return false;
  return typeof claims["sub"] === "string"
    && typeof claims["exp"] === "number"
    && claims["exp"] * 1000 > Date.now();
}

export function getRolesFromToken(token = getToken()): string[] {
  const claims = decodeToken(token);
  if (!claims) return [];
  const roles = [
    claims["roles"],
    claims["role"],
    claims["urn:logto:roles"],
    claims["http://logto.io/claims/roles"],
  ].flatMap(claimValues).map(value => value.trim().toLowerCase());
  const scopes = [
    claims["scope"],
    claims["scopes"],
    claims["scp"],
  ].flatMap(claimValues).map(value => value.trim().toLowerCase());
  if (scopes.includes("admin:all")) roles.push("administrator");
  return Array.from(new Set(roles));
}

function randomBase64Url(bytesLength: number): string {
  const bytes = new Uint8Array(bytesLength);
  crypto.getRandomValues(bytes);
  let binary = "";
  bytes.forEach(byte => { binary += String.fromCharCode(byte); });
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

async function sha256Base64Url(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  let binary = "";
  new Uint8Array(digest).forEach(byte => { binary += String.fromCharCode(byte); });
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

export async function beginLogtoLogin(nextPath = "/app/projects"): Promise<void> {
  if (typeof window === "undefined" || !isLogtoConfigured()) {
    throw new Error("Studio360 Logto runtime configuration is incomplete.");
  }
  const config = getLogtoConfig();
  const safeNext = nextPath.startsWith("/") && !nextPath.startsWith("//") ? nextPath : "/app/projects";
  const state = randomBase64Url(32);
  const verifier = randomBase64Url(64);
  writeSession(STATE_KEY, state);
  writeSession(VERIFIER_KEY, verifier);
  const params = new URLSearchParams({
    client_id: config.clientId,
    redirect_uri: config.redirectUri,
    response_type: "code",
    scope: "openid profile email offline_access roles admin:all",
    resource: config.resource,
    code_challenge: await sha256Base64Url(verifier),
    code_challenge_method: "S256",
    state,
  });
  writeSession("studio360.auth.return-url", safeNext);
  window.location.assign(`${config.issuer}/auth?${params.toString()}`);
}

export async function completeLogtoCallback(): Promise<{ username: string; roles: string[] } | null> {
  if (typeof window === "undefined" || !isLogtoConfigured()) return null;
  const current = new URL(window.location.href);
  const oauthError = current.searchParams.get("error");
  const code = current.searchParams.get("code");
  const state = current.searchParams.get("state");
  if (oauthError) {
    clearToken();
    removeSession(STATE_KEY);
    removeSession(VERIFIER_KEY);
    removeSession("studio360.auth.return-url");
    ["code", "state", "session_state", "error", "error_description", "iss"]
      .forEach(key => current.searchParams.delete(key));
    window.history.replaceState(
      window.history.state,
      document.title,
      `${current.pathname}${current.search}${current.hash}`,
    );
    throw new Error("Logto authorization was cancelled or denied.");
  }
  if (!code && !state) return null;

  const expectedState = readSession(STATE_KEY);
  const verifier = readSession(VERIFIER_KEY);
  removeSession(STATE_KEY);
  removeSession(VERIFIER_KEY);

  if (!code || !state || !expectedState || state !== expectedState || !verifier) {
    clearToken();
    throw new Error("Logto callback state is invalid or expired.");
  }

  const config = getLogtoConfig();
  const form = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: config.clientId,
    redirect_uri: config.redirectUri,
    code,
    code_verifier: verifier,
    resource: config.resource,
  });
  const response = await fetch(`${config.issuer}/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", Accept: "application/json" },
    body: form,
  });
  if (!response.ok) {
    clearToken();
    throw new Error("Logto token exchange failed.");
  }
  const payload = await response.json() as { access_token?: unknown; refresh_token?: unknown };
  if (typeof payload.access_token !== "string") {
    clearToken();
    throw new Error("Logto token response is missing an access token.");
  }
  setToken(payload.access_token);
  if (typeof payload.refresh_token === "string") setRefreshToken(payload.refresh_token);
  if (!isValidAccessToken(payload.access_token)) {
    clearToken();
    throw new Error("Logto access token claims are invalid.");
  }

  for (const key of ["code", "state", "session_state", "error", "error_description", "iss"]) {
    current.searchParams.delete(key);
  }
  window.history.replaceState(window.history.state, document.title, `${current.pathname}${current.search}${current.hash}`);
  const claims = decodeToken(payload.access_token) ?? {};
  const username = firstStringClaim(claims, ["name", "preferred_username", "email", "sub"]);
  return { username, roles: getRolesFromToken(payload.access_token) };
}

export function getPostLogtoLoginPath(fallback = "/app/projects"): string {
  const stored = readSession("studio360.auth.return-url");
  removeSession("studio360.auth.return-url");
  return stored && stored.startsWith("/") && !stored.startsWith("//") ? stored : fallback;
}

export function logtoLogoutUrl(): string | null {
  if (typeof window === "undefined" || !isLogtoConfigured()) return null;
  const config = getLogtoConfig();
  const postLogout = encodeURIComponent(window.location.origin);
  return `${config.issuer}/session/end?client_id=${encodeURIComponent(config.clientId)}&post_logout_redirect_uri=${postLogout}`;
}
