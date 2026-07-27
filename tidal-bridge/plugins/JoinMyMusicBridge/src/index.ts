import { LunaUnload, Tracer } from "@luna/core";
import { MediaItem, PlayState, redux, safeInterval, TidalApi } from "@luna/lib";
import { setPlayback, startServer, stopServer } from "./index.native";
import { settings } from "./Settings";

/**
 * Renderer half of the bridge. Reads playback state out of TIDAL's Redux store
 * and dispatches queue/skip actions, then hands the result to the main-process
 * HTTP server in index.native.ts.
 */

export const { trace } = Tracer("[JoinMyMusicBridge]");
export const unloads = new Set<LunaUnload>();
export { Settings } from "./Settings";

/** How often the playback snapshot (mainly progress) is refreshed. The backend
 *  polls every 5s and treats a >3s position jump as a seek, so 1s is ample. */
const SNAPSHOT_INTERVAL_MS = 1000;

/** TIDAL offers 80/160/320/640/1280. */
const COVER_SIZE = 320;

declare global {
	interface Window {
		__jmmBridge?: (data: { action: string;[key: string]: unknown }) => Promise<unknown>;
	}
}

const coverUrl = (uuid?: string | null, size = COVER_SIZE): string =>
	uuid ? `https://resources.tidal.com/images/${uuid.split("-").join("/")}/${size}x${size}.jpg` : "";

/** Cache of the resolved MediaItem for the playing track, so the 1s snapshot
 *  interval costs a Redux read rather than a lookup per tick. */
let cachedId = "";
let cachedItem: MediaItem | undefined;

const artistsOf = (raw: any): Array<{ id: string; name: string }> => {
	const list = raw?.artists?.length ? raw.artists : raw?.artist ? [raw.artist] : [];
	return list.map((a: any) => ({ id: String(a.id), name: a.name }));
};

/**
 * Build the now-playing payload in the shape the backend's poll loop expects,
 * or null when nothing is loaded.
 *
 * Reads the raw sync data off .tidalItem / .tidalAlbum rather than the class
 * accessors — those are async and MediaItem.title() does a MusicBrainz lookup.
 */
const snapshot = async (): Promise<Record<string, unknown> | null> => {
	const { playbackControls } = redux.store.getState();
	const ctx = playbackControls?.playbackContext;
	const songid = ctx?.actualProductId ? String(ctx.actualProductId) : "";
	if (!songid) return null;

	if (songid !== cachedId) {
		cachedItem = await MediaItem.fromId(songid, "track");
		cachedId = songid;
	}
	const raw: any = cachedItem?.tidalItem;
	if (!raw) return null;
	const album: any = raw.album ?? {};

	// PlayState.currentTime is the live player position in seconds (0 when
	// unavailable) — finer grained than Redux's latestCurrentTime.
	const progressSecs = PlayState.currentTime;
	const durationSecs = ctx?.actualDuration ?? raw.duration ?? 0;

	return {
		artist: artistsOf(raw),
		song: raw.title ?? "",
		album: album.title ?? "",
		songid,
		albumid: album.id != null ? String(album.id) : "",
		cover: coverUrl(album.cover),
		year: album.releaseYear ?? (album.releaseDate ?? "").slice(0, 4),
		playing: playbackControls?.playbackState === "PLAYING",
		duration_ms: Math.round(durationSecs * 1000),
		progress_ms: Math.round(progressSecs * 1000),
	};
};

const pushSnapshot = async () => {
	try {
		setPlayback(await snapshot());
	} catch (e) {
		trace.msg.err(`snapshot failed: ${e}`);
	}
};

/**
 * Catalog search. @luna/lib has no search helper, so this calls TIDAL's own v1
 * search with the client's credentials.
 *
 * Deliberately uses plain fetch rather than TidalApi.fetch: the latter is
 * memoized by URL for the whole session, which would serve stale results for a
 * repeated query.
 */
const search = async (query: string) => {
	const url =
		`https://desktop.tidal.com/v1/search?query=${encodeURIComponent(query)}` +
		`&limit=10&types=TRACKS&${TidalApi.queryArgs()}`;
	const res = await fetch(url, { headers: await TidalApi.getAuthHeaders() });
	if (!res.ok) throw new Error(`search failed: ${res.status} ${res.statusText}`);
	const data = await res.json();

	const items = data?.tracks?.items ?? [];
	return items.map((t: any) => ({
		id: String(t.id),
		name: t.title,
		artist: t.artists?.[0]?.name ?? t.artist?.name ?? "",
		album: t.album?.title ?? "",
		cover: coverUrl(t.album?.cover),
	}));
};

/** Track metadata for a request, in the shape the backend denormalises into the DB. */
const track = async (id: string) => {
	const item = await MediaItem.fromId(id, "track");
	const raw: any = item?.tidalItem;
	if (!raw) return null;
	const album: any = raw.album ?? {};
	return {
		song: raw.title ?? "",
		songid: String(raw.id ?? id),
		artist: artistsOf(raw),
		album: album.title ?? "",
		albumid: album.id != null ? String(album.id) : "",
		cover: coverUrl(album.cover),
	};
};

const actions: Record<string, (data: any) => unknown | Promise<unknown>> = {
	search: (d) => search(String(d.query)),
	track: (d) => track(String(d.id)),
	queue: (d) => {
		const id = String(d.id);
		// TIDAL resolves the id itself; the stub context is what the reference
		// plugin uses and is accepted.
		redux.actions["playQueue/ADD_LAST"]({
			context: { type: "UNKNOWN", id },
			mediaItemIds: [id],
		});
		return { queued: id };
	},
	next: () => {
		redux.actions["playQueue/MOVE_NEXT"]();
		return { skipped: true };
	},
};

window.__jmmBridge = async (data) => {
	const handler = actions[data.action];
	if (!handler) throw new Error(`Unknown action: ${data.action}`);
	const result = await handler(data);
	// Queue/skip change what's playing; refresh so the next poll isn't stale.
	if (data.action === "next") await pushSnapshot();
	return result;
};
unloads.add(() => {
	delete window.__jmmBridge;
});

startServer(settings.port);
unloads.add(() => stopServer());

let lastPort = settings.port;
safeInterval(unloads, () => {
	if (settings.port !== lastPort) {
		lastPort = settings.port;
		stopServer().then(() => startServer(settings.port));
		trace.msg.log(`Restarted bridge on port ${settings.port}`);
	}
}, 5000);

pushSnapshot();
MediaItem.onMediaTransition(unloads, pushSnapshot);
PlayState.onState(unloads, pushSnapshot);
safeInterval(unloads, pushSnapshot, SNAPSHOT_INTERVAL_MS);
