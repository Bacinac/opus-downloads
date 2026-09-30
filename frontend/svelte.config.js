import nodeAdapter from '@sveltejs/adapter-node';
import staticAdapter from '@sveltejs/adapter-static';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
import { csp } from './src/lib/opus/csp.js';

const demo = process.env.OPUS_DEMO === '1';

export default {
	preprocess: vitePreprocess(),
	kit: {
		adapter: demo ? staticAdapter({ fallback: 'index.html', precompress: true, strict: true }) : nodeAdapter(),
		files: demo ? { appTemplate: 'src/app.demo.html' } : {},
		...(demo ? {} : { csp })
	}
};
