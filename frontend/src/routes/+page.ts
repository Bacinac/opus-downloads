import { redirect } from '@sveltejs/kit';

// The engines and what they are doing: the first thing worth seeing.
export function load() {
	redirect(307, '/services');
}
