/* In-browser synthetic API for the public OPUS Downloads demo. */
(function () {
	'use strict';

	// The public demo speaks English until the visitor picks a language.
	try {
		if (!localStorage.getItem('locale')) localStorage.setItem('locale', 'en');
	} catch {
		/* no storage */
	}
	const realFetch = window.fetch.bind(window);
	const clone = (value) => JSON.parse(JSON.stringify(value));
	const answer = (body, status = 200) => new Response(JSON.stringify(body), {
		status,
		headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }
	});
	const now = '2026-09-30T19:50:00Z';
	let jobs = [
		{ id: 'job-demo-01', engine: 'sabnzbd', app: 'library', namespace: 'movies', title: 'Agent 327 Operation Barbershop (2017) 1080p WEB-DL', state: 'downloading', progress: 0.41, speed_bps: 31457280, eta_seconds: 19, detail: 'Downloading 412 / 1006 MB', landing_path: null, error: null, seen_at: now, created_at: '2026-09-30T19:42:00Z' },
		{ id: 'job-demo-02', engine: 'slskd', app: 'library', namespace: 'music', title: 'Jadranski Kolektiv — Između otoka [FLAC 24/96]', state: 'downloading', progress: 0.63, speed_bps: 5242880, eta_seconds: 96, detail: '5 of 8 files', landing_path: null, error: null, seen_at: now, created_at: '2026-09-30T18:10:00Z' },
		{ id: 'job-demo-03', engine: 'sabnzbd', app: 'library', namespace: 'music', title: 'Neon Delta — Night Grid [FLAC]', state: 'queued', progress: 0, speed_bps: null, eta_seconds: null, detail: 'Waiting for the engine', landing_path: null, error: null, seen_at: now, created_at: '2026-09-30T19:48:00Z' },
		{ id: 'job-demo-04', engine: 'ytdlp', app: 'library', namespace: 'video', title: 'Sound Workshop — Recording waves in the open', state: 'queued', progress: 0, speed_bps: null, eta_seconds: null, detail: 'Waiting for the engine', landing_path: null, error: null, seen_at: now, created_at: '2026-09-30T19:49:00Z' },
		{ id: 'job-demo-05', engine: 'qbittorrent', app: 'library', namespace: 'movies', title: 'Tears of Steel (2012) 2160p WEB-DL HEVC', state: 'complete', progress: 1, speed_bps: null, eta_seconds: null, detail: '1 file, 4.6 GB', landing_path: '/landing/movies/tears-of-steel', error: null, seen_at: '2026-09-29T22:04:00Z', created_at: '2026-09-29T21:35:00Z' },
		{ id: 'job-demo-06', engine: 'qbittorrent', app: 'library', namespace: 'series', title: 'Caminandes S01E03 Llamigos 1080p', state: 'complete', progress: 1, speed_bps: null, eta_seconds: null, detail: '1 file, 412 MB', landing_path: '/landing/series/caminandes', error: null, seen_at: '2026-09-28T17:12:00Z', created_at: '2026-09-28T17:01:00Z' },
		{ id: 'job-demo-07', engine: 'slskd', app: 'library', namespace: 'music', title: 'Mara Vida — Kava i kiša [FLAC 16/44]', state: 'complete', progress: 1, speed_bps: null, eta_seconds: null, detail: '8 files', landing_path: '/landing/music/mara-vida', error: null, seen_at: '2026-09-27T10:38:00Z', created_at: '2026-09-27T10:10:00Z' },
		{ id: 'job-demo-08', engine: 'ytdlp', app: null, namespace: 'manual', title: 'Sound Workshop — How an analogue synthesiser is made', state: 'failed', progress: 0.21, speed_bps: null, eta_seconds: null, detail: '', landing_path: null, error: 'The source removed the original upload.', seen_at: '2026-09-26T21:20:00Z', created_at: '2026-09-26T21:18:00Z' }
	];
	const engines = [
		{ name: 'prowlarr', kind: 'indexer', family: 'indexer_mediated', mode: 'external', url: 'http://prowlarr.lan:9696', public_url: 'https://prowlarr.demo.invalid', enabled: true, can_search: true, can_link: false, can_grab: false, use_vpn: false, ui_url: '', health: { ok: true, detail: '6 indexers available' }, container: null },
		{ name: 'sabnzbd', kind: 'usenet', family: 'indexer_mediated', mode: 'external', url: 'http://sabnzbd.lan:8080', public_url: '', enabled: true, can_search: true, can_link: false, can_grab: true, use_vpn: false, ui_url: '', health: { ok: true, detail: 'Connected · 31.5 MB/s' }, container: null },
		{ name: 'qbittorrent', kind: 'torrent', family: 'indexer_mediated', mode: 'external', url: 'http://qbittorrent.lan:8080', public_url: '', enabled: true, can_search: true, can_link: false, can_grab: true, use_vpn: true, ui_url: '', health: { ok: true, detail: 'Connected through the VPN' }, container: null },
		{ name: 'slskd', kind: 'soulseek', family: 'direct_source', mode: 'external', url: 'http://slskd.lan:5030', public_url: '', enabled: true, can_search: true, can_link: false, can_grab: true, use_vpn: true, ui_url: '', health: { ok: true, detail: 'Soulseek network available' }, container: null },
		{ name: 'ytdlp', kind: 'web', family: 'direct_source', mode: 'in_process', url: '', public_url: '', enabled: true, can_search: false, can_link: false, can_grab: true, use_vpn: false, ui_url: '', health: { ok: true, detail: 'Web fetch ready' }, container: null }
	];
	const field = (key, group, value, kind = 'text', options = [], secret = false, scope = 'both') => ({ key: `${group}_${key}`, group, label: key, kind, secret, options, scope, value: secret ? '' : value, is_set: secret ? true : null });
	let settings = [
		field('landing_dir', 'content', '/landing'), field('folders', 'content', 'music,movies,series,video', 'list'),
		field('provider', 'vpn', 'wireguard', 'select', ['wireguard', 'openvpn']), field('config', 'vpn', 'set', 'text', [], true),
		field('mode', 'prowlarr', 'external', 'select', ['bundled', 'external']), field('url', 'prowlarr', 'http://prowlarr.lan:9696'), field('api_key', 'prowlarr', 'set', 'text', [], true),
		field('mode', 'sabnzbd', 'external', 'select', ['bundled', 'external']), field('url', 'sabnzbd', 'http://sabnzbd.lan:8080'), field('api_key', 'sabnzbd', 'set', 'text', [], true),
		field('mode', 'qbittorrent', 'external', 'select', ['bundled', 'external']), field('url', 'qbittorrent', 'http://qbittorrent.lan:8080'), field('username', 'qbittorrent', 'opus'), field('password', 'qbittorrent', 'set', 'text', [], true),
		field('mode', 'slskd', 'external', 'select', ['bundled', 'external']), field('url', 'slskd', 'http://slskd.lan:5030'), field('api_key', 'slskd', 'set', 'text', [], true),
		field('enabled', 'ytdlp', 'true', 'bool')
	];
	const releases = [
		{ title: 'Agent.327.Operation.Barbershop.2017.2160p.WEB-DL.HEVC', protocol: 'usenet', source: 'sabnzbd', grab_ref: { engine: 'sabnzbd', id: 'demo-1' }, size: 18360985190, seeders: null, speed_bps: null, queue_length: null, age_days: 2, bitrate: null, subs_hint: true, indexer: 'Demo Index', tracks: null, bit_depth: null, sample_rate: null, categories: [2040] },
		{ title: 'Agent 327 Operation Barbershop 2017 1080p BluRay', protocol: 'torrent', source: 'qbittorrent', grab_ref: { engine: 'qbittorrent', id: 'demo-2' }, size: 29420525977, seeders: 84, speed_bps: null, queue_length: null, age_days: 12, bitrate: null, subs_hint: false, indexer: 'Open Demo', tracks: null, bit_depth: null, sample_rate: null, categories: [2000] },
		{ title: 'Jadranski Kolektiv - Svjetla na rivi [FLAC 24-96]', protocol: 'soulseek', source: 'slskd', grab_ref: { engine: 'slskd', id: 'demo-3' }, size: 1451229184, seeders: null, speed_bps: 5242880, queue_length: 1, age_days: null, bitrate: 3200, subs_hint: null, indexer: '', tracks: 9, bit_depth: 24, sample_rate: 96000, categories: [] },
	];
	async function bodyOf(input, init) {
		const body = init && init.body !== undefined ? init.body : input instanceof Request ? await input.clone().text() : '';
		try { return typeof body === 'string' && body ? JSON.parse(body) : {}; } catch (_) { return {}; }
	}
	async function api(path, method, input, init) {
		const body = await bodyOf(input, init);
		if (path === '/api/version') return realFetch('/demo-version.json', { cache: 'no-store' });
		if (path === '/api/auth/session') return answer({ required: true, authenticated: true, username: 'demo', role: 'admin' });
		if (path === '/api/auth/login' || path === '/api/auth/logout' || path === '/api/auth/password') return answer({ ok: true });
		if (path === '/api/auth/token') return answer({ token: method === 'POST' ? 'opus_demo_new_token' : 'opus_demo_service_token' });
		if (path === '/api/auth/people') return answer({ people: [{ name: 'demo', display: 'Demo administrator', role: 'admin', disabled: false }, { name: 'obitelj', display: 'Family member', role: 'user', disabled: false }] });
		if (path === '/api/auth/devices') return answer({ devices: [{ id: 1, name: 'OPUS TV · living room', module: 'player', collected: true, seen_at: now }] });
		if (path.startsWith('/api/auth/')) return answer({ ok: true });
		if (path === '/api/words') return answer({ hr: {}, en: {} });
		if (path === '/api/engines') return answer(clone(engines));
		if (path.startsWith('/api/engines/')) return answer({ ok: true });
		if (path === '/api/settings') {
			if (method === 'PUT') settings = settings.map((s) => body[s.key] === undefined || s.secret ? s : { ...s, value: String(body[s.key]) });
			return answer(clone(settings));
		}
		if (path === '/api/jobs') return answer(clone(jobs));
		let match = path.match(/^\/api\/jobs\/([^/]+)\/history$/);
		if (match) return answer([{ kind: 'accepted', detail: 'Library handed over the request.', created_at: '2026-09-19T08:42:00Z' }, { kind: 'started', detail: 'The engine accepted the job.', created_at: '2026-09-19T08:42:04Z' }, { kind: jobs.find((j) => j.id === match[1])?.state === 'failed' ? 'failed' : 'state', detail: 'Latest demo state.', created_at: now }]);
		match = path.match(/^\/api\/jobs\/([^/]+)$/);
		if (match && method === 'DELETE') { jobs = jobs.filter((j) => j.id !== match[1]); return answer({ ok: true }); }
		if (match) return answer(clone(jobs.find((j) => j.id === match[1]) || jobs[0]));
		if (path === '/api/search') return answer({ releases: clone(releases), errors: [{ engine: 'prowlarr', detail: 'Demo: one indexer did not answer; the other sources did.' }] });
		if (path === '/api/grab') {
			const id = 'job-demo-' + String(jobs.length + 5).padStart(2, '0');
			jobs.unshift({ id, engine: body.grab_ref?.engine || 'sabnzbd', app: null, namespace: body.namespace || 'manual', title: 'Hand-picked demo release', state: 'queued', progress: 0, speed_bps: null, eta_seconds: null, detail: 'Waiting for the engine', landing_path: null, error: null, seen_at: now, created_at: now });
			return answer({ job_id: id });
		}
		return answer({ detail: `Demo fixture is missing ${method} ${path}` }, 404);
	}
	window.fetch = async function (input, init) {
		const url = new URL(input instanceof Request ? input.url : String(input), location.href);
		if (url.origin !== location.origin || !url.pathname.startsWith('/api/')) return realFetch(input, init);
		return api(url.pathname, (init?.method || (input instanceof Request ? input.method : 'GET')).toUpperCase(), input, init);
	};
})();
