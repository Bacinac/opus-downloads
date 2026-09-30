import { moduleName, modulesFor } from '$lib/opus';

export const MODULE = moduleName('downloads');

export const MODULES = modulesFor('downloads', {
	library: import.meta.env.VITE_OPUS_LIBRARY_URL,
	player: import.meta.env.VITE_OPUS_PLAYER_URL
});
