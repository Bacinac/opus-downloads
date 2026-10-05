import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vitest/config';

// the hostname a tunnel publishes this dev server at, when it is opened from
// beyond the LAN; hot reload then comes back over the tunnel's TLS as well
const devHost = process.env.OPUS_DEV_HOST;

export default defineConfig({
	plugins: [sveltekit()],
	resolve: process.env.VITEST ? { conditions: ['browser'] } : undefined,
	test: {
		include: ['src/**/*.test.ts']
	},
	server: {
		host: '0.0.0.0',
		port: 5173,
		allowedHosts: [...(devHost ? [devHost] : []), 'localhost', '.local'],
		proxy: {
			// ws so dev matches what server.mjs does in production
			'/api': { target: 'http://backend:8097', ws: true }
		},
		hmr: devHost ? { host: devHost, clientPort: 443, protocol: 'wss' } : undefined
	}
});
