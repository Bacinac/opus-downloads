<script lang="ts">
	import { About } from '$lib/opus';
	import { t } from '$lib/i18n';
	import { MODULE } from '$lib/core/modules';
</script>

<About
	module={MODULE}
	tagline={t('about.tagline')}
	what={t('about.what')}
	why={t('about.why')}
	stack={t('about.stack')}
/>
