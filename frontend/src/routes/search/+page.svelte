<script lang="ts">
	// The Search page: a query, a type and the engines to ask, posted to the
	// unified /search. A failed engine is shown beside the results the other
	// engines returned. Grabbing one posts its grab_ref straight back — the ref
	// names its own engine.

	import { t, type MessageKey } from '$lib/i18n';
	import {
		Button,
		Notice,
		PageHead,
		Picks,
		SearchBox,
		Tag,
		formatBytes,
		formatNumber,
		json,
		plural,
		request,
		toasts
	} from '$lib/kit';
	import { engines } from '$lib/engines/engines.svelte';
	import type { Release, SearchFailure, SearchResponse } from '$lib/core/types';

	const TYPES = ['any', 'movie', 'tv', 'music'] as const;

	let query = $state('');
	let chosen = $state<string[]>(['any']);
	let searchers = $derived(engines.list.filter((e) => e.can_search && e.enabled));
	// every engine that can answer until the admin chooses otherwise
	let picked = $state<string[] | null>(null);
	let asked = $derived(picked ?? searchers.map((e) => e.name));
	// the per-app isolation key: it becomes the engine's own category, so the
	// grabbed files land in their own subtree of the landing zone
	let namespace = $state('manual');
	let searching = $state(false);
	let grabbing = $state<number | null>(null);
	let results = $state<Release[] | null>(null);
	let errors = $state<SearchFailure[]>([]);
	let failure = $state<string | null>(null);

	async function run() {
		if (!query.trim() || !asked.length) return;
		searching = true;
		failure = null;
		errors = [];
		const answer = await request<SearchResponse>('/api/search', json({ query, type: chosen[0], engines: asked }), {
			failed: (detail) => (failure = detail)
		});
		if (answer) {
			results = answer.releases;
			errors = answer.errors;
		}
		searching = false;
	}

	async function grab(release: Release, index: number) {
		grabbing = index;
		const job = await request<{ job_id: string }>(
			'/api/grab',
			json({ grab_ref: release.grab_ref, namespace })
		);
		if (job) toasts.success(t('search.grabbed', { id: job.job_id.slice(0, 8) }));
		grabbing = null;
	}

	$effect(() => {
		engines.load();
	});
</script>

<PageHead sticky={false}>
	{#snippet ways()}
		<Picks picks={TYPES.map((ty) => ({ key: ty, label: t(`search.type.${ty}` as MessageKey) }))} bind:chosen />
	{/snippet}
</PageHead>

<div class="page">
	<SearchBox
		bind:value={query}
		placeholder={t('search.placeholder')}
		action={searching ? t('search.searching') : t('search.button')}
		busy={searching || !query.trim() || !asked.length}
		onsubmit={run}
	>
		{#snippet after()}
			<label class="namespace">
				<span>{t('search.namespace.label')}</span>
				<input bind:value={namespace} />
			</label>
		{/snippet}
	</SearchBox>

	<div class="engines">
		<Picks
			picks={searchers.map((e) => ({ key: e.name, label: t(`engine.${e.name}` as MessageKey) }))}
			bind:chosen={() => asked, (next) => (picked = next)}
			many
			all={t('search.engines.all')}
		/>
	</div>

	{#if failure !== null}
		<Notice tone="err"><p>{failure}</p></Notice>
	{:else if results !== null}
		{#if errors.length}
			<Notice tone="err">
				<p>{t('search.partialFailures')}</p>
				<ul>
					{#each errors as error}
						<li>{t(`engine.${error.engine}` as MessageKey)}: {error.detail}</li>
					{/each}
				</ul>
			</Notice>
		{/if}
		{#if results.length === 0}
			<p class="muted">{t('search.noResults')}</p>
		{:else}
			<div class="table-scroll">
				<table>
					<thead>
						<tr>
							<th>{t('search.col.title')}</th>
							<th>{t('search.col.protocol')}</th>
							<th>{t('search.col.source')}</th>
							<th>{t('search.col.size')}</th>
							<th>{t('search.col.seeders')}</th>
							<th>{t('search.col.indexer')}</th>
							<th></th>
						</tr>
					</thead>
					<tbody>
						{#each results as r, i}
							<tr>
								<td class="wrap">
									{r.title}
									{#if r.tracks !== null || (r.bit_depth !== null && r.sample_rate !== null)}
										<span class="facts">
											{#if r.tracks !== null}
												<Tag>{plural(r.tracks, 'release.tracks.one', 'release.tracks.few', 'release.tracks.many')}</Tag>
											{/if}
											{#if r.bit_depth !== null && r.sample_rate !== null}
												<Tag>
													{t('release.format', {
														depth: formatNumber(r.bit_depth),
														rate: formatNumber(r.sample_rate / 1000, { maximumFractionDigits: 1 })
													})}
												</Tag>
											{/if}
										</span>
									{/if}
								</td>
								<td>{t(`kind.${r.protocol}` as MessageKey)}</td>
								<td>{t(`engine.${r.source}` as MessageKey)}</td>
								<td>{r.size ? formatBytes(r.size) : '—'}</td>
								<td>{r.seeders === null ? '—' : formatNumber(r.seeders)}</td>
								<td>{r.indexer || '—'}</td>
								<td class="actions">
									<Button
										tone="primary"
										disabled={grabbing !== null || !namespace.trim()}
										onclick={() => grab(r, i)}
									>
										{grabbing === i ? t('search.grabbing') : t('search.grab')}
									</Button>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			</div>
		{/if}
	{/if}
</div>

<style>
	.engines {
		margin: 0.75rem 0;
	}
	.namespace {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		font-size: var(--fs-m);
		color: var(--muted);
	}
	.namespace input {
		width: 11rem;
	}
	.facts {
		display: flex;
		flex-wrap: wrap;
		gap: 0.3rem;
		margin-top: 0.3rem;
	}
	td.actions {
		text-align: right;
		white-space: nowrap;
	}
</style>
