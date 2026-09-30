<script lang="ts">
	// The Settings page: the whole catalog, one card per engine, each with its
	// live availability and its editable config, over the one draft the page
	// saves.

	import { t } from '$lib/i18n';
	import { Card, SaveBar, SettingField, SettingsDraft } from '$lib/kit';
	import EngineCard from '$lib/engines/EngineCard.svelte';
	import { engines } from '$lib/engines/engines.svelte';
	import type { Setting } from '$lib/core/types';

	const form = new SettingsDraft<Setting>();

	$effect(() => {
		form.load();
		engines.load();
	});

	async function save() {
		if (await form.save()) await engines.reload();
	}
</script>

<div class="page">
	<form onsubmit={(e) => { e.preventDefault(); save(); }}>
		<div class="shared">
			<Card title={t('content.title')} collapsible>
				<div class="fields">
					{#each form.of('content') as f (f.key)}
						<SettingField setting={f} bind:value={form.draft[f.key]} />
					{/each}
				</div>
			</Card>

			<Card title={t('vpn.title')} collapsible>
				<div class="fields">
					{#each form.of('vpn') as f (f.key)}
						<SettingField setting={f} bind:value={form.draft[f.key]} />
					{/each}
				</div>
			</Card>
		</div>

		<div class="cards">
			{#each engines.list as engine (engine.name)}
				<EngineCard {engine} fields={form.of(engine.name)} draft={form.draft} onchanged={() => engines.reload()} />
			{/each}
		</div>

		<SaveBar dirty={form.dirty} saving={form.saving} />
	</form>
</div>

<style>
	.cards {
		display: grid;
		gap: 1.25rem;
	}
	.shared {
		display: grid;
		gap: 1.25rem;
		margin-bottom: 1.25rem;
	}
</style>
