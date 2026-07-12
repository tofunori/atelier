async function request(path, init) {
    const response = await fetch(path, {
        cache: "no-store",
        ...init,
        headers: { Accept: "application/json", ...(init?.headers ?? {}) },
    });
    const payload = (await response.json());
    if (!response.ok)
        throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
}
function sessionKey(project) {
    return `atelierGallerySession:${encodeURIComponent(project)}`;
}
export const AtelierClient = {
    request,
    health: () => request("/ping"),
    status: () => request("/agent-status?limit=50"),
    provenance: (rel) => request(`/provenance?rel=${encodeURIComponent(rel)}`),
    loadSession(project) {
        try {
            return JSON.parse(localStorage.getItem(sessionKey(project)) || "{}");
        }
        catch {
            return {};
        }
    },
    saveSession(project, session) {
        try {
            localStorage.setItem(sessionKey(project), JSON.stringify(session));
        }
        catch {
            // Private browsing or a restricted embedded surface: session persistence is optional.
        }
    },
};
window.AtelierClient = AtelierClient;
window.dispatchEvent(new CustomEvent("atelier-client-ready", { detail: AtelierClient }));
