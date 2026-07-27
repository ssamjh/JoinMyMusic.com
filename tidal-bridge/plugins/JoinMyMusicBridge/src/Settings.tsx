import { ReactiveStore } from "@luna/core";
import { LunaSettings, LunaTextSetting } from "@luna/ui";
import React from "react";

const debounce = <T extends (...args: any[]) => void>(fn: T, ms: number) => {
	let timer: ReturnType<typeof setTimeout>;
	return (...args: Parameters<T>) => {
		clearTimeout(timer);
		timer = setTimeout(() => fn(...args), ms);
	};
};

// 24124 rather than 24123, so this can coexist with @vmohammad/api.
export const settings = await ReactiveStore.getPluginStorage("joinmymusic-bridge", {
	port: 24124,
});

export const Settings = () => {
	const [port, setPort] = React.useState(settings.port);
	const commit = React.useMemo(
		() =>
			debounce((newPort: number) => {
				if (Number.isNaN(newPort) || newPort < 1 || newPort > 65535) {
					setPort(settings.port);
					return;
				}
				settings.port = newPort;
			}, 500),
		[port]
	);
	return (
		<LunaSettings>
			<LunaTextSetting
				title="Bridge port"
				desc="Port the JoinMyMusic backend connects to (default 24124)"
				value={port}
				type="number"
				onChange={(e: React.ChangeEvent<HTMLInputElement>) => {
					setPort(Number(e.target.value));
					commit(Number(e.target.value));
				}}
			/>
		</LunaSettings>
	);
};
