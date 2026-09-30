import { beforeEach, describe, expect, test, vi } from 'vitest';
import type { EngineInfo } from '$lib/core/types';

const answers = vi.hoisted(() => [] as ((list: unknown) => void)[]);
vi.mock('$lib/kit', () => ({
	request: vi.fn(() => new Promise((answer) => answers.push(answer)))
}));

import { request } from '$lib/kit';
import { engines } from './engines.svelte';

const said = (name: string) => [{ name }] as unknown as EngineInfo[];

beforeEach(() => {
	answers.length = 0;
	vi.mocked(request).mockClear();
});

describe('the engine list', () => {
	test('loads asked at once are asked once', async () => {
		const first = engines.load();
		const second = engines.load();
		expect(request).toHaveBeenCalledOnce();
		answers[0](said('sabnzbd'));
		await Promise.all([first, second]);
		expect(engines.list).toEqual(said('sabnzbd'));
		expect(engines.answered).toBe(true);
	});

	test('an answer to an older question does not put the old state back', async () => {
		const before = engines.reload();
		const after = engines.reload();
		answers[1](said('after'));
		await after;
		answers[0](said('before'));
		await before;
		expect(engines.list).toEqual(said('after'));
	});
});
