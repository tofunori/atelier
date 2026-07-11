export type WatcherStatus = {
  enabled: boolean;
  running: boolean;
  lastScan?: number;
  lastBuild?: number;
  lastEventAt?: number;
  lastBuildAt?: number;
  lastChanged?: string[];
  error?: string;
};

export type Health = {
  ok: boolean;
  service?: string;
  backend?: "rust" | string;
  project?: string;
  revision?: number;
  agentHost?: string | null;
  agentBridgeProtocol?: number;
  agentInbox?: number;
  watcher?: WatcherStatus;
};

export type Provenance = {
  generator?: string;
  command?: string[];
  inputs?: string[];
  gitCommit?: string;
  confidence?: "declared" | "same-stem" | string;
};

export type GallerySession = {
  query?: string;
  sort?: string;
  folder?: string;
  scrollY?: number;
  activeRel?: string | null;
};

type AtelierWindow = Window & {
  AtelierClient?: typeof AtelierClient;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    cache: "no-store",
    ...init,
    headers: { Accept: "application/json", ...(init?.headers ?? {}) },
  });
  const payload = (await response.json()) as T & { error?: string };
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

function sessionKey(project: string): string {
  return `atelierGallerySession:${encodeURIComponent(project)}`;
}

export const AtelierClient = {
  request,
  health: () => request<Health>("/ping"),
  status: () => request<Record<string, unknown>>("/agent-status?limit=50"),
  provenance: (rel: string) =>
    request<{ ok: boolean; rel: string; provenance?: Provenance }>(
      `/provenance?rel=${encodeURIComponent(rel)}`,
    ),
  loadSession(project: string): GallerySession {
    try {
      return JSON.parse(localStorage.getItem(sessionKey(project)) || "{}") as GallerySession;
    } catch {
      return {};
    }
  },
  saveSession(project: string, session: GallerySession): void {
    try {
      localStorage.setItem(sessionKey(project), JSON.stringify(session));
    } catch {
      // Private browsing or a restricted embedded surface: session persistence is optional.
    }
  },
};

(window as AtelierWindow).AtelierClient = AtelierClient;
window.dispatchEvent(new CustomEvent("atelier-client-ready", { detail: AtelierClient }));
