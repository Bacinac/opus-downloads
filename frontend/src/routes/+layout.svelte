<script lang="ts">
	import '../app.css';
	import { page } from '$app/state';
	import { Toasts, onUnauthorized } from '$lib/kit';
	import { Login, Shell, icons, me, type Alert } from '$lib/opus';
	import { t, type MessageKey } from '$lib/i18n';
	import { MODULES, MODULE } from '$lib/core/modules';
	import { engines } from '$lib/engines/engines.svelte';

	let { children } = $props();
	const demo = import.meta.env.VITE_OPUS_DEMO === '1';

	onUnauthorized(() => (me.open = false));

	$effect(() => {
		me.check();
	});

	$effect(() => {
		if (me.open) engines.load();
	});

	// a tunnel carrying nothing is true wherever you are standing, so it is said
	// wherever you are standing rather than only on the two pages that happen to
	// know about engines
	const alerts = $derived<Alert[]>([
		...(demo ? [{ key: 'demo', message: t('demo.banner'), tone: 'quiet' as const }] : []),
		...engines.list
			.filter((e) => e.container?.vpn?.leaking)
			.map((e) => ({
				key: e.name,
				href: '/settings',
				message: t('alert.vpnLeaking', { engine: t(`engine.${e.name}` as MessageKey) })
			}))
	]);

	// re-derived so the labels follow a language switch; the shell takes them
	// already translated because the catalogue is this module's, not the package's
	const nav = $derived([
		{ href: '/services', label: t('nav.services'), icon: icons.services },
		{ href: '/jobs', label: t('nav.jobs'), icon: icons.jobs },
		{ href: '/search', label: t('nav.search'), icon: icons.search },
		{ href: '/settings', label: t('nav.settings'), icon: icons.settings }
	]);
</script>

{#if me.open === false}
	<Login module={MODULE} onin={() => me.check()} />
	<Toasts />
{:else if me.open}
	<Shell
		module={MODULE}
		{nav}
		pathname={page.url.pathname}
		modules={MODULES}
		{alerts}
		owner={me.admin}
		account={me.name ? { username: me.name, href: '/account', onlogout: () => me.logout() } : undefined}
	>
		{@render children()}
	</Shell>
{:else}
	<Toasts />
{/if}

