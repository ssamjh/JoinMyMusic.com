/**
 * Minimal ambient types for the sliver of Electron this plugin uses.
 *
 * `electron` is marked external by Luna's build and is provided by the host
 * process at runtime, so depending on the real (very large) electron package
 * just for types isn't worth it.
 */
declare module "electron" {
	interface WebContents {
		executeJavaScript(code: string): Promise<any>;
		send(channel: string, ...args: any[]): void;
	}
	class BrowserWindow {
		static fromId(id: number): BrowserWindow | null;
		webContents: WebContents;
	}
}
