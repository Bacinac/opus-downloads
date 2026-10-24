<script lang="ts">
	import { About } from '#lib/opus/index.js';
	import { t } from '#lib/i18n/index.js';
	import { MODULE } from '#lib/core/modules.js';
</script>

<About
	module={MODULE}
	tagline={t('about.tagline')}
	what={t('about.what')}
	why={t('about.why')}
	stack={t('about.stack')}
/>
