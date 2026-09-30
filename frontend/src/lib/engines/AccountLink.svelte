<script lang="ts">
	// An engine with no credential to type: the service hands a token to whoever
	// completes a device-code approval, so the card offers a conversation instead
	// of a field. Ask for a link, the user approves it on the service's own site,
	// and the answer arrives on a later poll.

	import { t } from '$lib/i18n';
	import { ArmedButton, Button, Notice, Tag, request } from '$lib/kit';

	let { engine, onchanged }: { engine: string; onchanged: () => void } = $props();

	const path = $derived(`/api/engines/${engine}/link`);

	type LinkState = {
		linked: boolean;
		account: string;
		pending: boolean;
		url: string | null;
		error: string | null;
	};

	let link = $state<LinkState | null>(null);
	let working = $state(false);
	let timer: ReturnType<typeof setTimeout> | null = null;

	async function load() {
		const next = await request<LinkState>(path);
		if (!next) return;
		const settled = link?.pending && !next.pending;
		link = next;
		// the approval happens in another tab, so the only way to learn it landed
		// is to keep asking while it is outstanding
		if (next.pending) timer = setTimeout(load, 2000);
		else if (settled) onchanged();
	}

	async function start() {
		working = true;
		const next = await request<LinkState>(path, { method: 'POST' });
		if (next) {
			link = next;
			if (next.url) window.open(next.url, '_blank', 'noopener');
			if (next.pending) timer = setTimeout(load, 2000);
		}
		working = false;
	}

	async function unlink() {
		working = true;
		const next = await request<LinkState>(path, { method: 'DELETE' });
		if (next) {
			link = next;
			onchanged();
		}
		working = false;
	}

	$effect(() => {
		load();
		return () => {
			if (timer) clearTimeout(timer);
		};
	});
</script>

{#if link}
	{#if link.error}
		<Notice tone="err"><p>{link.error}</p></Notice>
	{/if}
	<div class="link">
		<Tag tone={link.linked ? 'ok' : link.pending ? 'busy' : 'quiet'}>
			{link.linked
				? t('link.linkedAs', { account: link.account })
				: link.pending
					? t('link.waiting')
					: t('link.notLinked')}
		</Tag>
		{#if link.pending && link.url}
			<a href={link.url} target="_blank" rel="noopener">{t('link.openApproval')}</a>
		{/if}
		<span class="spacer"></span>
		{#if link.linked}
			<ArmedButton disabled={working} onconfirm={unlink}>{t('link.unlink')}</ArmedButton>
		{:else}
			<Button tone="primary" disabled={working || link.pending} onclick={start}>
				{working ? t('common.saving') : t('link.link')}
			</Button>
		{/if}
	</div>
{/if}

<style>
	.link {
		display: flex;
		align-items: center;
		gap: 0.6rem;
		flex-wrap: wrap;
	}
	a {
		font-size: var(--fs-s);
		color: var(--accent);
	}
	.spacer {
		flex: 1;
	}
</style>
