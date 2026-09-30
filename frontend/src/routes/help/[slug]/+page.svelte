<script lang="ts">
	import { page } from '$app/state';
	import { HelpPage } from '$lib/kit';
	import { helpFor } from '$lib/opus';
</script>

<HelpPage help={helpFor('downloads')} slug={page.params.slug ?? ''} />
