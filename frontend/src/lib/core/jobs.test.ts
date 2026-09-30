import { describe, expect, test } from 'vitest';
import { isActive, isStale, merged } from './jobs';
import type { Job } from './types';

const NOW = Date.parse('2026-09-23T12:00:00Z');

const job = (j: Partial<Job>): Job => ({
	id: 'j1',
	engine: 'sabnzbd',
	app: null,
	namespace: 'library',
	title: 'A job',
	state: 'downloading',
	progress: 0.5,
	speed_bps: null,
	eta_seconds: null,
	detail: '',
	landing_path: null,
	error: null,
	seen_at: null,
	created_at: '2026-09-23T10:00:00Z',
	...j
});

describe('active', () => {
	test('is queued or downloading, nothing that has ended', () => {
		expect(isActive(job({ state: 'queued' }))).toBe(true);
		expect(isActive(job({ state: 'downloading' }))).toBe(true);
		expect(isActive(job({ state: 'complete' }))).toBe(false);
		expect(isActive(job({ state: 'failed' }))).toBe(false);
	});
});

describe('stale', () => {
	test('is an active job its engine has not reported for over five minutes', () => {
		expect(isStale(job({ seen_at: '2026-09-23T11:54:59Z' }), NOW)).toBe(true);
		expect(isStale(job({ seen_at: '2026-09-23T11:56:00Z' }), NOW)).toBe(false);
	});

	test('is never a job that has ended, nor one never seen at all', () => {
		expect(isStale(job({ state: 'complete', seen_at: '2026-09-22T00:00:00Z' }), NOW)).toBe(false);
		expect(isStale(job({}), NOW)).toBe(false);
	});
});

describe('the list, fetched again', () => {
	test('keeps what the engine last said about a job on screen', () => {
		const shown = [job({ speed_bps: 5e6, eta_seconds: 90, detail: 'repairing', progress: 0.5 })];
		const [row] = merged([job({ progress: 0.6 })], shown);
		expect(row).toMatchObject({ progress: 0.6, speed_bps: 5e6, eta_seconds: 90, detail: 'repairing' });
	});

	test('takes a new job as stored and drops one no longer stored', () => {
		const rows = merged([job({ id: 'j2', detail: 'fresh' })], [job({ detail: 'old' })]);
		expect(rows).toEqual([job({ id: 'j2', detail: 'fresh' })]);
	});
});
