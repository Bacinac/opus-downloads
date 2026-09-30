// Every engine and its state, held once: the layout and the pages under it show
// the same list, and every load of it probes every engine.

import { request } from '$lib/kit';
import type { EngineInfo } from '$lib/core/types';

class EngineStore {
	list = $state<EngineInfo[]>([]);
	// an empty list before the answer arrives is not an empty list: drawn as one,
	// it reads as a verdict about the install rather than a page still loading
	answered = $state(false);
	#pending: Promise<void> | null = null;
	#asked = 0;

	/** Joins a load already on its way. */
	load(): Promise<void> {
		this.#pending ??= this.reload().finally(() => (this.#pending = null));
		return this.#pending;
	}

	/** Asks again, for after something changed: an answer to a question put
	 *  before the change would put the old state back on screen. */
	async reload(): Promise<void> {
		const asked = ++this.#asked;
		const data = await request<EngineInfo[]>('/api/engines');
		if (data && asked === this.#asked) this.list = data;
		this.answered = true;
	}
}

export const engines = new EngineStore();
