import { BrowserWindow } from "electron";
import { createServer, IncomingMessage, Server, ServerResponse } from "http";

/**
 * Main-process half of the bridge: a small JSON HTTP API the JoinMyMusic
 * backend calls.
 *
 * Binds 0.0.0.0 deliberately — the backend runs in Docker Desktop and reaches
 * the host as host.docker.internal, which does NOT reach a 127.0.0.1 bind.
 * There is no auth: this listens only on a secured subnet. No CORS headers are
 * sent either, which is what stops a random web page you browse to from
 * driving the client — the JSON content type forces a preflight that fails.
 */

let server: Server | null = null;

/** Last playback snapshot pushed from the renderer, so GET /playback is instant. */
let playback: Record<string, unknown> | null = null;

const EMPTY_CURRENT = {
	artist: [],
	song: "",
	album: "",
	songid: "",
	albumid: "",
	cover: "",
	year: "",
	playing: false,
	duration_ms: 0,
	progress_ms: 0,
};

const json = (res: ServerResponse, status: number, body: unknown) => {
	res.writeHead(status, { "Content-Type": "application/json" });
	res.end(JSON.stringify(body));
};

/**
 * Run an action in the renderer, where the Redux store and TIDAL's own
 * authenticated fetch live. Mirrors how the reference @vmohammad/api plugin
 * bridges main -> renderer.
 */
const invokeRenderer = async (action: string, params: Record<string, unknown>): Promise<unknown> => {
	const win = BrowserWindow.fromId(1);
	if (!win) throw new Error("TIDAL window not available");
	return win.webContents.executeJavaScript(
		`window.__jmmBridge?.(${JSON.stringify({ action, ...params })})`
	);
};

const readBody = (req: IncomingMessage): Promise<Record<string, unknown>> =>
	new Promise((resolve, reject) => {
		let body = "";
		req.on("data", (chunk) => {
			body += chunk;
			if (body.length > 1e6) reject(new Error("Body too large"));
		});
		req.on("end", () => {
			try {
				resolve(body ? JSON.parse(body) : {});
			} catch {
				reject(new Error("Invalid JSON"));
			}
		});
		req.on("error", reject);
	});

const handle = async (req: IncomingMessage, res: ServerResponse) => {
	const url = new URL(req.url || "/", `http://${req.headers.host}`);
	const path = url.pathname.replace(/\/+$/, "") || "/";
	const method = req.method ?? "GET";

	try {
		if (method === "GET" && path === "/health") {
			// playback is only non-null once the renderer has pushed a snapshot,
			// which it cannot do until TIDAL has a session.
			return json(res, 200, { ok: true, loggedIn: playback !== null });
		}

		if (method === "GET" && path === "/playback") {
			return json(res, 200, { current: playback ?? EMPTY_CURRENT });
		}

		if (method === "GET" && path === "/search") {
			const q = url.searchParams.get("q") ?? "";
			if (q.trim().length < 2) return json(res, 400, { error: "Query too short" });
			return json(res, 200, { results: await invokeRenderer("search", { query: q }) });
		}

		if (method === "GET" && path.startsWith("/track/")) {
			const id = decodeURIComponent(path.slice("/track/".length));
			if (!id) return json(res, 400, { error: "No track id" });
			const track = await invokeRenderer("track", { id });
			if (!track) return json(res, 404, { error: "Track not found" });
			return json(res, 200, track);
		}

		if (method === "POST" && path === "/queue") {
			const body = await readBody(req);
			const id = String(body.id ?? "");
			if (!id) return json(res, 400, { error: "No track id" });
			await invokeRenderer("queue", { id });
			return json(res, 200, { ok: true });
		}

		if (method === "POST" && path === "/next") {
			await invokeRenderer("next", {});
			return json(res, 200, { ok: true });
		}

		return json(res, 404, { error: "Not found" });
	} catch (e) {
		json(res, 500, { error: e instanceof Error ? e.message : "Bridge error" });
	}
};

export const setPlayback = (snapshot: Record<string, unknown> | null) => {
	playback = snapshot;
};

export const startServer = async (port: number) => {
	await stopServer();
	server = createServer(handle);
	server.on("error", (e) => console.error("[JoinMyMusicBridge] server error:", e));
	server.listen(port, "0.0.0.0", () =>
		console.log(`[JoinMyMusicBridge] listening on 0.0.0.0:${port}`)
	);
};

export const stopServer = async () => {
	if (!server) return;
	const s = server;
	server = null;
	playback = null;
	await new Promise<void>((resolve) => s.close(() => resolve()));
	console.log("[JoinMyMusicBridge] stopped");
};
