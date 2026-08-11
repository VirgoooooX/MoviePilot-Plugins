<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import CollectionManager from './CollectionManager.vue'
import { cloneConfig, unwrapResponse } from '../provider'

const props = defineProps({ api: { type: Object, default: () => ({}) }, pluginId: { type: String, default: 'EmbyTmdbCollectionSync' }, hideTitle: Boolean })
const loading = ref(false)
const saving = ref(false)
const error = ref('')
const status = ref({ config: {}, servers: [], libraries: [], plan: {}, job: {} })
const config = ref({ enabled: false, show_sidebar_nav: true, server: '', libraries: [], overwrite_images: true, delete_empty: true, sync_logo: true })
let timer = null
const base = computed(() => `plugin/${props.pluginId || 'EmbyTmdbCollectionSync'}`)

// 加载插件状态并在任务运行时持续轮询。
async function loadStatus(resetConfig = false) {
  loading.value = true
  try {
    const response = await props.api.get(`${base.value}/status`)
    status.value = unwrapResponse(response) || status.value
    if (resetConfig || !config.value.server && !(config.value.libraries || []).length) {
      config.value = cloneConfig(status.value.config || config.value)
    }
    error.value = ''
  } catch (err) { error.value = err?.message || '加载失败' } finally { loading.value = false }
}

// 保存插件配置。
async function saveConfig() {
  saving.value = true
  try {
    const response = await props.api.post(`${base.value}/config`, config.value)
    status.value = unwrapResponse(response) || status.value
    config.value = cloneConfig(status.value.config || config.value)
    error.value = ''
    return true
  } catch (err) { error.value = err?.message || '保存失败' } finally { saving.value = false }
  return false
}

// 启动只读预演。
async function scan() {
  // 保存失败时不得继续扫描，避免使用页面上未落盘的服务器或电影库范围。
  if (!await saveConfig()) return false
  try { unwrapResponse(await props.api.post(`${base.value}/scan`, {})); await loadStatus() } catch (err) { error.value = err?.message || '预演启动失败' }
  return !error.value
}

// 执行用户审核并选择的计划。
async function apply(payload) {
  try { unwrapResponse(await props.api.post(`${base.value}/apply`, payload)); await loadStatus(); return true } catch (err) { error.value = err?.message || '执行启动失败'; return false }
}

// 把预演中的 TMDB 合集缺片交给 MoviePilot 原生电影订阅链。
async function subscribe(payload) {
  try { unwrapResponse(await props.api.post(`${base.value}/subscribe`, payload)); await loadStatus(); return true } catch (err) { error.value = err?.message || '缺片订阅启动失败'; return false }
}

// 解除人工保护后立即重新预演；真正写入 Emby 仍需用户审核执行。
async function restore(payload) {
  try {
    unwrapResponse(await props.api.post(`${base.value}/customization`, payload))
    unwrapResponse(await props.api.post(`${base.value}/scan`, {}))
    await loadStatus()
    return true
  } catch (err) { error.value = err?.message || '恢复 TMDB 管理失败'; return false }
}

// 请求取消扫描或执行任务。
async function cancel() {
  try { unwrapResponse(await props.api.post(`${base.value}/cancel`, {})); await loadStatus(); return true } catch (err) { error.value = err?.message || '取消任务失败'; return false }
}

defineExpose({ loadStatus, saveConfig, loading, saving })
onMounted(async () => { await loadStatus(true); timer = window.setInterval(() => { if (status.value?.job?.running || status.value?.job?.busy) loadStatus(false) }, 2500) })
onBeforeUnmount(() => timer && window.clearInterval(timer))
</script>

<template>
  <VAlert v-if="error" type="error" variant="tonal" class="ma-4" :text="error" />
  <CollectionManager :status="status" :config="config" :loading="loading" :saving="saving" :hide-title="hideTitle" @refresh="loadStatus" @save="saveConfig" @scan="scan" @apply="apply" @subscribe="subscribe" @restore="restore" @cancel="cancel" />
</template>
