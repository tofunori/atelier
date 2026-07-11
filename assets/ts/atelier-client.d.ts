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
    backend?: "python" | "rust" | string;
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
declare function request<T>(path: string, init?: RequestInit): Promise<T>;
export declare const AtelierClient: {
    request: typeof request;
    health: () => Promise<Health>;
    status: () => Promise<Record<string, unknown>>;
    provenance: (rel: string) => Promise<{
        ok: boolean;
        rel: string;
        provenance?: Provenance;
    }>;
    loadSession(project: string): GallerySession;
    saveSession(project: string, session: GallerySession): void;
};
export {};
