<script lang="ts">
	// The list is the stored rows, asked for again on every tick so a job started
	// elsewhere appears; each unfinished job is then refreshed through /jobs/{id},
	// which is what actually asks the owning engine.

	import { t, type MessageKey } from '$lib/i18n';
	import {
		ArmedButton,
		Notice,
		Progress,
		Tag,
		duration,
		formatBytes,
		formatDateTime,
		request,
		toasts,
		type TagTone
	} from '$lib/kit';
	import { isActive, isStale, merged } from '$lib/core/jobs';
	import type { Job, JobEvent } from '$lib/core/types';

	const POLL_MS = 4000;

	let jobs = $state<Job[]>([]);
	let busy = $state<string | null>(null);
	let unanswered = $state<Record<string, string>>({});
	let unlisted = $state<string | null>(null);
	let histories = $state<Record<string, JobEvent[] | undefined>>({});

	const TONES: Record<Job['state'], TagTone> = {
		queued: 'quiet',
		downloading: 'busy',
		complete: 'ok',
		failed: 'err'
	};
	const EVENT_LABELS: Record<JobEvent['kind'], MessageKey> = {
		accepted: 'jobs.event.accepted',
		started: 'jobs.event.started',
		state: 'jobs.event.state',
		failed: 'jobs.event.failed'
	};

	let answered = $state(false);

	function listed(stored: Job[]) {
		jobs = merged(stored, jobs);
		for (const id of Object.keys(unanswered)) if (!jobs.some((job) => job.id === id)) delete unanswered[id];
	}

	async function load() {
		try {
			const data = await request<Job[]>('/api/jobs');
			if (data) listed(data);
		} finally {
			answered = true;
		}
	}

	// each refresh asks every engine behind the list, which can take longer than
	// the interval; one still on its way is not asked again on top of itself
	let refreshing = false;

	async function refresh() {
		if (refreshing) return;
		refreshing = true;
		try {
			const stored = await request<Job[]>('/api/jobs', {}, { failed: (detail) => (unlisted = detail) });
			if (!stored) return;
			unlisted = null;
			listed(stored);
			const fresh = await Promise.all(
				jobs.filter(isActive).map((job) =>
					request<Job>(`/api/jobs/${job.id}`, {}, {
						failed: (detail) => (unanswered[job.id] = detail)
					})
				)
			);
			const byId = new Map(fresh.filter(Boolean).map((job) => [job!.id, job!]));
			for (const id of byId.keys()) delete unanswered[id];
			jobs = jobs.map((job) => byId.get(job.id) ?? job);
		} finally {
			refreshing = false;
		}
	}

	async function drop(job: Job) {
		busy = job.id;
		if ((await request(`/api/jobs/${job.id}`, { method: 'DELETE' })) !== null) {
			delete unanswered[job.id];
			toasts.success(isActive(job) ? t('jobs.cancelled') : t('jobs.deleted'));
			await load();
		}
		busy = null;
	}

	async function loadHistory(job: Job) {
		if (histories[job.id] !== undefined) return;
		const events = await request<JobEvent[]>(`/api/jobs/${job.id}/history`);
		if (events) histories[job.id] = events;
	}

	$effect(() => {
		load();
		const timer = setInterval(refresh, POLL_MS);
		return () => clearInterval(timer);
	});
</script>

<div class="page">
	{#if unlisted !== null}
		<Notice tone="err"><p>{unlisted}</p></Notice>
	{/if}
	{#if !answered}
		<p class="muted">{t('common.loading')}</p>
	{:else if jobs.length === 0}
		<p class="muted">{t('jobs.empty')}</p>
	{:else}
		<div class="table-scroll">
			<table>
				<thead>
					<tr>
						<th>{t('jobs.col.title')}</th>
						<th>{t('jobs.col.engine')}</th>
						<th>{t('jobs.col.namespace')}</th>
						<th>{t('jobs.col.state')}</th>
						<th>{t('jobs.col.progress')}</th>
						<th></th>
					</tr>
				</thead>
				<tbody>
					{#each jobs as job (job.id)}
						<tr>
							<td class="wrap">
								<span class="title">{job.title || job.id}</span>
								{#if unanswered[job.id]}
									<span class="error">{unanswered[job.id]}</span>
								{/if}
								{#if job.error}
									<span class="error">{job.error}</span>
								{:else if job.landing_path}
									<span class="path">{t('jobs.landing')} {job.landing_path}</span>
								{/if}
								<details class="history" ontoggle={() => loadHistory(job)}>
									<summary>{t('jobs.history')}</summary>
									{#if histories[job.id]}
										<ul>
											{#each histories[job.id]! as event}
												<li>
													<span>{formatDateTime(event.created_at)} · {t(EVENT_LABELS[event.kind])}</span>
													{#if event.detail}<span class="error">{event.detail}</span>{/if}
												</li>
											{/each}
										</ul>
									{/if}
								</details>
							</td>
							<td>{t(`engine.${job.engine}` as MessageKey)}</td>
							<td class="muted-cell">{job.namespace}</td>
							<td>
								<Tag tone={TONES[job.state]}>{t(`jobs.state.${job.state}` as MessageKey)}</Tag>
							</td>
							<td class="progress-cell">
								{#if isActive(job)}
									<Progress value={job.progress} share />
									<span class="sub">
										{#if job.speed_bps}{formatBytes(job.speed_bps)}/s{/if}
										{#if job.eta_seconds}· {t('jobs.eta', { time: duration(job.eta_seconds) })}{/if}
									</span>
								{:else}
									<span class="sub">{job.detail || '—'}</span>
								{/if}
								{#if job.seen_at}
									<span class="seen">{t('jobs.lastSeen', { time: formatDateTime(job.seen_at) })}</span>
								{/if}
								{#if isStale(job)}
									<span class="stale">{t('jobs.stale')}</span>
								{/if}
							</td>
							<td class="actions">
								<ArmedButton disabled={busy === job.id} onconfirm={() => drop(job)}>
									{isActive(job) ? t('jobs.cancel') : t('jobs.delete')}
								</ArmedButton>
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{/if}
</div>

<style>
	.title {
		display: block;
	}
	.path,
	.error,
	.sub {
		display: block;
		font-size: var(--fs-s);
		color: var(--muted);
		margin-top: 0.2rem;
	}
	.error {
		color: var(--danger);
	}
	.history {
		margin-top: 0.4rem;
		font-size: var(--fs-s);
		color: var(--muted);
	}
	.history summary {
		cursor: pointer;
	}
	.history ul {
		margin: 0.35rem 0 0;
		padding-left: 1rem;
	}
	.history li + li {
		margin-top: 0.25rem;
	}
	.seen {
		display: block;
		margin-top: 0.25rem;
		color: var(--muted);
		font-size: var(--fs-s);
	}
	.stale {
		display: block;
		margin-top: 0.2rem;
		color: var(--warn);
		font-size: var(--fs-s);
	}
	.muted-cell {
		color: var(--muted);
	}
	.progress-cell {
		min-width: 190px;
	}
	td.actions {
		text-align: right;
		white-space: nowrap;
	}
</style>
