<script setup>
import { onMounted, ref } from 'vue'
import CollectionManager from './CollectionManager.vue'
import { cloneConfig } from '../provider'
const props = defineProps({ initialConfig: { type: Object, default: () => ({}) } })
const emit = defineEmits(['save', 'close'])
const config = ref({})
const status = ref({ config: {}, servers: [], libraries: [], plan: {}, job: {} })
// 通知宿主保存配置弹窗中的设置。
function save() { emit('save', cloneConfig(config.value)) }
onMounted(() => { config.value = cloneConfig(props.initialConfig); status.value.config = config.value })
</script>
<template>
  <div>
    <VToolbar density="comfortable"><div class="text-h6 ms-3">Emby TMDB 合集设置</div><VSpacer /><VBtn icon="mdi-content-save" color="primary" @click="save" /><VBtn icon="mdi-close" @click="emit('close')" /></VToolbar>
    <VDivider />
    <VCardText>
      <VSwitch v-model="config.enabled" label="启用插件" color="primary" />
      <VSwitch v-model="config.show_sidebar_nav" label="显示侧栏入口" color="primary" />
      <VSwitch v-model="config.overwrite_images" label="更新已接管合集的封面和徽标" color="primary" />
      <VSwitch v-model="config.sync_logo" label="同步合集徽标" color="primary" />
      <VSwitch v-model="config.delete_empty" label="删除插件管理的空合集" color="primary" />
      <VAlert type="info" variant="tonal" text="服务器、电影库选择和变更审核请在插件侧栏工作台中完成；合集图片只使用 TMDB，支持 zh-CN、zh-SG、zh-TW、zh-HK 地区标签。" />
    </VCardText>
  </div>
</template>
