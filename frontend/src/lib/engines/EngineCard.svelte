<script lang="ts">
	// One engine's card on the Settings page: its identity (name, kind, family),
	// its live availability badge, what it can do (search / grab) and its
	// editable config fields. Unavailable engines are shown, not hidden — the
	// badge and hint say why. The health detail is a backend diagnostic surfaced
	// as a tooltip, not translated copy.

	import { t, type MessageKey } from '$lib/i18n';
	import { Button, Card, Notice, SettingField, Tag, request } from '$lib/kit';
	import AccountLink from '$lib/engines/AccountLink.svelte';
	import type { EngineInfo, Setting } from '$lib/core/types';

	let {
		engine,
		fields,
		draft,
		onchanged
	}: {
		engine: EngineInfo;
		fields: Setting[];
		draft: Record<string, string>;
		onchanged: () => void;
	} = $props();

	let working = $state(false);

	// the container is OPUS's only while the SAVED mode is bundled; acting on a
	// draft the user has not committed would start something they did not ask for
	let bundled = $derived(engine.mode === 'bundled');
	let running = $derived(engine.container?.running ?? false);
	let leftover = $derived(!bundled && engine.container !== null);

	async function lifecycle(action: 'start' | 'stop') {
		working = true;
		const done = await request(`/api/engines/${engine.name}/${action}`, { method: 'POST' });
		if (done) onchanged();
		working = false;
	}

	// Ask only for what the chosen mode needs. The mode being edited is the
	// draft's, not the saved one, so the fields follow the switch immediately
	// instead of after a save.
	let mode = $derived(draft[`${engine.name}_mode`] ?? engine.mode);
	let shown = $derived(fields.filter((f) => f.scope === 'both' || f.scope === mode));
</script>

<div class="engine" class:off={!engine.enabled}>
	<Card collapsible>
		{#snippet heading()}
			<div class="titles">
				<h2>{t(`engine.${engine.name}` as MessageKey)}</h2>
				<div class="tags">
					<Tag>{t(`kind.${engine.kind}` as MessageKey)}</Tag>
					<Tag tone="quiet">{t(`engines.family.${engine.family}` as MessageKey)}</Tag>
					{#if engine.can_search}<Tag tone="busy">{t('engines.caps.search')}</Tag>{/if}
					{#if engine.can_grab}<Tag tone="busy">{t('engines.caps.grab')}</Tag>{/if}
					{#if engine.use_vpn}<Tag tone="ok">{t('engines.caps.vpn')}</Tag>{/if}
				</div>
			</div>
		{/snippet}
		{#snippet actions()}
			<Tag tone={engine.enabled ? 'ok' : 'warn'} title={engine.health.detail}>
				{engine.enabled ? t('engines.available') : t('engines.unavailable')}
			</Tag>
		{/snippet}

		{#if !engine.enabled}
			<Notice tone="warn"><p>{t('engines.unavailableHint')}</p></Notice>
		{/if}

		{#if bundled || leftover}
			{#if engine.container?.error}
				<Notice tone="err"><p>{engine.container.error}</p></Notice>
			{/if}
			<div class="lifecycle">
				<Tag tone={running ? 'ok' : 'quiet'}>
					{running
						? t('engines.container.running')
						: engine.container?.present
							? t('engines.container.stopped')
							: t('engines.container.absent')}
				</Tag>
				{#if engine.container?.vpn?.running}
					{@const tunnel = engine.container.vpn}
					<Tag tone={tunnel.leaking === false ? 'ok' : 'warn'}>
						{tunnel.leaking
							? t('engines.vpn.leaking')
							: tunnel.leaking === false
								? t('engines.vpn.exit', { ip: tunnel.exit_ip ?? '' })
								: t('engines.vpn.unknown')}
					</Tag>
				{/if}
				<span class="spacer"></span>
				<Button disabled={working} onclick={() => lifecycle(running || leftover ? 'stop' : 'start')}>
					{working
						? t('common.saving')
						: running || leftover
							? t('engines.container.stop')
							: t('engines.container.start')}
				</Button>
			</div>
		{/if}

		{#if engine.can_link}
			<AccountLink engine={engine.name} {onchanged} />
		{/if}

		<div class="fields">
			{#each shown as f (f.key)}
				<SettingField setting={f} bind:value={draft[f.key]} />
			{/each}
		</div>
	</Card>
</div>

<style>
	.engine.off {
		opacity: 0.82;
	}
	/* the name and what the engine is read as one line: the tags qualify the
	   name, and stacked under it they doubled the height of every folded card */
	.titles {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 0.5rem 0.75rem;
		min-width: 0;
	}
	h2 {
		margin: 0;
		font-size: var(--fs-l);
	}
	.tags {
		display: flex;
		flex-wrap: wrap;
		gap: 0.35rem;
	}
	.lifecycle {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 0.6rem;
	}
	.spacer {
		flex: 1;
	}
</style>
