<script setup>
import { ref } from 'vue'
import AppPage from './AppPage.vue'
defineProps({ api: { type: Object, default: () => ({}) } })
const emit = defineEmits(['close'])
const page = ref(null)
</script>
<template>
  <div>
    <VToolbar density="comfortable"><div class="text-h6 ms-3">Emby TMDB 合集整理</div><VSpacer /><VBtn icon="mdi-refresh" :loading="page?.loading" @click="page?.loadStatus()" /><VBtn icon="mdi-close" @click="emit('close')" /></VToolbar>
    <VDivider />
    <AppPage ref="page" :api="api" plugin-id="EmbyTmdbCollectionSync" hide-title />
  </div>
</template>
