<script lang="ts">
	// Every engine, reached the only way each of them can be. One tab each for
	// the ones with an interface to open, and one tab for all the rest together
	// — an engine running inside OPUS has nothing to open, so a tab of its own
	// would be a door onto a sentence.

	import { t, type MessageKey } from '$lib/i18n';
	import { Notice, Tabs, Tag, type Tab } from '$lib/kit';
	import { engines } from '$lib/engines/engines.svelte';

	const INSIDE = '#inside';
	const ADOPTED = '#adopted';

	let active = $state<string | null>(null);

	let framed = $derived(engines.list.filter((e) => e.mode === 'bundled'));
	let inside = $derived(engines.list.filter((e) => e.mode === 'in_process' && e.enabled));
	let adopted = $derived(engines.list.filter((e) => e.mode === 'external'));

	let tabs = $derived<Tab[]>([
		...framed.map((e) => ({
			key: e.name,
			label: t(`engine.${e.name}` as MessageKey),
			warn: !e.container?.running || !e.health.ok,
			// a tunnel is only vouched for once it is known not to leak
			badge: e.container?.vpn
				? { text: 'VPN', tone: e.container.vpn.leaking === false ? ('ok' as const) : ('warn' as const) }
				: undefined
		})),
		...(inside.length ? [{ key: INSIDE, label: t('services.inside') }] : []),
		...(adopted.length ? [{ key: ADOPTED, label: t('services.adopted') }] : [])
	]);

	let picked = $derived(active ?? tabs[0]?.key ?? null);
	let current = $derived(framed.find((e) => e.name === picked));
	let listed = $derived(picked === INSIDE ? inside : picked === ADOPTED ? adopted : []);

	$effect(() => {
		engines.load();
	});
</script>

<div class="page">
	{#if !engines.answered}
		<p class="muted">{t('common.loading')}</p>
	{:else if tabs.length === 0}
		<p class="muted">{t('services.none')}</p>
	{:else}
		<Tabs {tabs} active={picked} onpick={(key) => (active = key)} />

		<div class="body">
			{#if current}
				{#if current.container?.vpn?.leaking || (current.container?.vpn?.running && current.container.vpn.leaking === null)}
					<div class="said">
						<Notice tone="warn">
							<p>{current.container.vpn.leaking ? t('engines.vpn.leaking') : t('engines.vpn.unknown')}</p>
						</Notice>
					</div>
				{/if}
				{#if current.container?.running && !current.health.ok}
					<div class="said">
						<Notice tone="warn">
							<p>{t('engines.unavailable')}: {current.health.detail}</p>
						</Notice>
					</div>
				{/if}
				{#if current.container?.running}
					{#if current.ui_embeddable}
						<iframe
							title={current.name}
							src={current.ui_url}
							sandbox="allow-scripts allow-same-origin allow-forms allow-modals allow-popups allow-popups-to-escape-sandbox allow-downloads"
						></iframe>
					{:else}
						<div class="said">
							<p>{t('services.ownPage')}</p>
							<p><a class="at" href={current.ui_url} target="_blank" rel="noreferrer">
								{t('services.open', { name: t(`engine.${current.name}` as MessageKey) })}
							</a></p>
						</div>
					{/if}
				{:else}
					<p class="standing">{t('services.stopped')}</p>
				{/if}
			{:else}
				<ul>
					{#each listed as e (e.name)}
						<li>
							<span class="name">{t(`engine.${e.name}` as MessageKey)}</span>
							<Tag tone={e.health.ok ? 'ok' : 'warn'}>
								{e.health.ok ? t('engines.available') : t('engines.unavailable')}
							</Tag>
							<span class="detail">{e.health.detail}</span>
							{#if e.mode === 'external'}
								{@const at = e.public_url || e.url}
								<a class="at" href={at} target="_blank" rel="noreferrer">{at}</a>
								{#if !e.public_url}<Tag tone="quiet">{t('services.lanOnly')}</Tag>{/if}
							{/if}
						</li>
					{/each}
				</ul>
			{/if}
		</div>
	{/if}
</div>

<style>
	.body {
		border: 1px solid var(--border);
		border-radius: 0 8px 8px 8px;
		background: var(--surface);
	}
	iframe {
		display: block;
		width: 100%;
		height: calc(100vh - 285px);
		min-height: 480px;
		border: none;
		border-radius: 0 8px 8px 8px;
		background: var(--surface);
	}
	ul {
		list-style: none;
		margin: 0;
		padding: 0.4rem 0;
	}
	li {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 0.75rem;
		padding: 0.7rem 1.35rem;
	}
	li + li {
		border-top: 1px solid var(--border);
	}
	.standing {
		margin: 0;
		padding: 1.1rem 1.35rem;
		color: var(--muted);
	}
	.name {
		font-weight: 600;
		min-width: 7rem;
	}
	.detail {
		color: var(--muted);
		font-size: var(--fs-m);
	}
	.at {
		color: var(--accent);
		font-size: var(--fs-m);
		word-break: break-all;
	}
	.said {
		padding: 0.9rem 1.35rem 0;
	}
</style>
