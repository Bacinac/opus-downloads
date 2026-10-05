// The shapes the backend serves. One declaration per response body, so a field
// that moves in the API breaks here and nowhere else. Unions are transcribed
// from the backend enums — a value the API cannot emit must not typecheck.

export type EngineKind = 'indexer' | 'usenet' | 'torrent' | 'soulseek' | 'stream' | 'web';
export type EngineMode = 'bundled' | 'external' | 'in_process';
export type EngineFamily = 'indexer_mediated' | 'direct_source';
export type Protocol = 'usenet' | 'torrent' | 'soulseek' | 'stream' | 'web';

export type EngineHealth = { ok: boolean; detail: string };

export type ContainerState = {
	present: boolean;
	running: boolean;
	ours?: boolean;
	status?: string;
	image?: string;
	error?: string;
	// a tunnel only: the address the engine actually comes out at, and whether
	// that turned out to be the install's own — null when it could not be told
	exit_ip?: string | null;
	leaking?: boolean | null;
	vpn?: ContainerState;
};

export type EngineInfo = {
	name: string;
	kind: EngineKind;
	family: EngineFamily;
	mode: EngineMode;
	// where the engine answers, and where a person opens it — the same until an
	// adopted instance is published somewhere of its own. Both empty for a
	// direct source, which has no address at all.
	url: string;
	public_url: string;
	enabled: boolean;
	can_search: boolean;
	can_link: boolean;
	can_grab: boolean;
	use_vpn: boolean;
	// where a bundled engine's own interface opens, on the engines' own origin
	ui_url: string;
	ui_embeddable: boolean;
	health: EngineHealth;
	// a bundled engine's container, or the one an engine that left bundled mode
	// could not take down
	container: ContainerState | null;
};

export type Setting = {
	key: string;
	group: string;              // the owning engine's name
	label: string;              // field suffix, resolved to an i18n label
	kind: 'text' | 'bool' | 'select' | 'list';
	secret: boolean;
	options: string[];
	scope: 'both' | 'external' | 'bundled';
	value: string;
	is_set: boolean | null;
};

export type JobState = 'queued' | 'downloading' | 'complete' | 'failed';

export type Job = {
	id: string;
	engine: string;
	app: string | null;
	namespace: string;
	title: string;
	state: JobState;
	progress: number;
	speed_bps: number | null;
	eta_seconds: number | null;
	detail: string;
	landing_path: string | null;
	error: string | null;
	seen_at: string | null;
	created_at: string;
};

export type JobEvent = {
	kind: 'accepted' | 'started' | 'state' | 'failed';
	detail: string;
	created_at: string;
};

export type Release = {
	title: string;
	protocol: Protocol;
	source: string;
	grab_ref: Record<string, unknown>;
	size: number | null;
	seeders: number | null;
	speed_bps: number | null;
	queue_length: number | null;
	age_days: number | null;
	bitrate: number | null;
	subs_hint: boolean | null;
	indexer: string;
	tracks: number | null;
	bit_depth: number | null;
	sample_rate: number | null;
	categories: number[];
};

export type SearchFailure = {
	engine: string;
	detail: string;
};

export type SearchResponse = {
	releases: Release[];
	errors: SearchFailure[];
};
