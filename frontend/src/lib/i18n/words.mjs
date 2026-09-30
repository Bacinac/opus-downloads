#!/usr/bin/env node
// Downloads' words, checked by the package's checker against what this
// repository actually says: each family below names where its members come
// from, most of them the backend's own vocabularies. Run over the repository by
// ./check.sh.

import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { checkWords, quotedIn, report } from '../kit/words/check.mjs';

// This file sits at <root>/frontend/src/lib/i18n/.
const root = join(new URL('.', import.meta.url).pathname, '../../../..');
const read = (path) => readFileSync(join(root, path), 'utf8');
const from = (path, pattern) => quotedIn(root, path, pattern);
const all = (path, pattern) => [...read(path).matchAll(pattern)].map((m) => m[1]);
const enumValues = (path, name) =>
	all(path, new RegExp(`class ${name}\\(enum\\.StrEnum\\):([\\s\\S]*?)\\n\\n\\n`, 'g')).flatMap((body) =>
		[...body.matchAll(/= "(\w+)"/g)].map((m) => m[1])
	);

const CATALOG = 'backend/opus/engines/catalog.py';
const SPEC = 'backend/opus/engines/spec.py';
const SETTINGS = 'backend/opus/settings_store.py';
const MODELS = 'backend/opus/models.py';
const PATHS = 'backend/opus/paths.py';
const block = (path, start) => {
	const said = read(path);
	const at = said.indexOf(start);
	return at < 0 ? '' : said.slice(at, said.indexOf('\n)\n', at));
};
// what a person is asked on the Settings page: every field of the engines and
// of the vpn and content groups, less what OPUS learns or is handed rather than
// typed — and less the access group, which belongs to the account page
const typed = (text) =>
	[...text.replace(/learned=\([\s\S]*?\),\n/g, '').matchAll(/CredField\("(\w+)"([^)]*)\)/g)]
		.filter((m) => !/hidden=True/.test(m[2]))
		.map((m) => m[1]);

const families = {
	engine: { where: `the engines in ${CATALOG}`, members: () => all(CATALOG, /^\s+name="(\w+)",$/gm) },
	'engines.family': {
		where: `the families in ${CATALOG}`,
		members: () => all(CATALOG, /^\s+family="(\w+)",$/gm)
	},
	kind: {
		where: `EngineKind in ${SPEC} and Protocol in backend/opus/engines/base.py`,
		members: () => [...enumValues(SPEC, 'EngineKind'), ...enumValues('backend/opus/engines/base.py', 'Protocol')]
	},
	'jobs.state': { where: `JobState in ${MODELS}`, members: () => enumValues(MODELS, 'JobState') },
	'search.type': {
		where: 'TYPES in routes/search/+page.svelte',
		members: () => from('frontend/src/routes/search/+page.svelte', /const TYPES = \[([^\]]*)\]/)
	},
	field: {
		where: `the fields of ${CATALOG} and the per-engine settings of ${SETTINGS}`,
		members: () => [
			...typed(block(CATALOG, 'BUILT_IN: tuple[EngineSpec, ...] = (')),
			...typed(block(CATALOG, 'VPN_FIELDS: tuple[CredField, ...] = (')),
			...typed(block(CATALOG, 'CONTENT_FIELDS: tuple[CredField, ...] = (')),
			...all(SETTINGS, /SettingSpec\(f"\{e\.name\}_\w+", e\.name,[^)]*?label="(\w+)"/g)
		]
	},
	'settings.opt': {
		where: `MODES in ${SETTINGS}, the options of the one select setting`,
		members: () => from(SETTINGS, /^MODES = \(([^)]*)\)/m).map((mode) => `mode.${mode}`)
	},
	'settings.err': {
		where: `the refusals raised for a field in ${SETTINGS} and the path refusals of ${PATHS}`,
		members: () => [
			...all(SETTINGS, /SettingsValidationError\(spec\.key, "(\w+)"\)/g),
			...all(PATHS, /(?:return|else) "(\w+)"/g)
		]
	}
};

report(checkWords({ root, packages: ['frontend/src/lib/kit', 'frontend/src/lib/opus'], families }));
