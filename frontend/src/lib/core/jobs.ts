import type { Job } from './types';

const STALE_AFTER_MS = 5 * 60 * 1000;

export const isActive = (job: Job) => job.state === 'queued' || job.state === 'downloading';

export const isStale = (job: Job, now = Date.now()) =>
	isActive(job) && Boolean(job.seen_at) && now - Date.parse(job.seen_at!) > STALE_AFTER_MS;

/** A stored row carries nothing live, so what the engine last said about a job
 *  already on screen stays until the engine is asked again. */
export function merged(stored: Job[], shown: Job[]): Job[] {
	const was = new Map(shown.map((job) => [job.id, job]));
	return stored.map((job) => {
		const live = was.get(job.id);
		return live ? { ...job, speed_bps: live.speed_bps, eta_seconds: live.eta_seconds, detail: live.detail } : job;
	});
}
