<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  status: { type: Object, default: () => ({}) },
  config: { type: Object, default: () => ({}) },
  loading: Boolean,
  saving: Boolean,
  hideTitle: Boolean,
})
const emit = defineEmits(['refresh', 'save', 'scan', 'apply'])

const tab = ref('plan')
const confirmDialog = ref(false)
const selected = ref([])
const adopted = ref([])
const plan = computed(() => props.status?.plan || {})
const rows = computed(() => plan.value?.collections || [])
const summary = computed(() => plan.value?.summary || {})
const job = computed(() => props.status?.job || {})
const serverLibraries = computed(() => (props.status?.libraries || []).filter(item => !props.config.server || item.server === props.config.server))
const libraryOptions = computed(() => serverLibraries.value.map(item => ({ title: `${item.name}（${item.count || 0}）`, value: item.id })))
const serverOptions = computed(() => props.status?.servers || [])

// 选择所有存在实际变更的合集。
function selectAllChanges() {
  selected.value = rows.value.filter(row => row.create || row.add?.length || row.remove?.length || row.poster || row.logo).map(row => String(row.key))
}

// 打开高风险执行确认框。
function requestApply() {
  if (!selected.value.length) return
  confirmDialog.value = true
}

// 提交审核过的合集计划。
function confirmApply() {
  confirmDialog.value = false
  emit('apply', { selected: selected.value, adopted: adopted.value })
}
</script>

<template>
  <div class="collection-manager pa-4 pa-md-6">
    <div v-if="!hideTitle" class="d-flex flex-wrap align-center ga-3 mb-5">
      <div>
        <div class="text-h5 font-weight-bold">Emby TMDB 合集整理</div>
        <div class="text-body-2 text-medium-emphasis">先生成只读预演，再审核并执行合集成员与图片变更。</div>
      </div>
      <VSpacer />
      <VBtn prepend-icon="mdi-refresh" variant="tonal" :loading="loading" @click="emit('refresh')">刷新</VBtn>
    </div>

    <VAlert v-if="job.error" type="warning" variant="tonal" class="mb-4" :text="job.error" />
    <VCard v-if="job.running" variant="tonal" class="mb-5">
      <VCardText>
        <div class="d-flex justify-space-between mb-2"><span>{{ job.message }}</span><span>{{ job.progress || 0 }}%</span></div>
        <VProgressLinear :model-value="job.progress || 0" color="primary" rounded />
        <div class="text-caption text-medium-emphasis mt-2">
          阶段：{{ job.phase || '-' }}<span v-if="job.total"> · {{ job.current || 0 }}/{{ job.total }}</span><span v-if="job.run_id"> · {{ job.run_id }}</span>
        </div>
      </VCardText>
    </VCard>

    <VCard variant="outlined" class="mb-5">
      <VCardTitle>运行范围</VCardTitle>
      <VCardText>
        <VRow>
          <VCol cols="12" md="5"><VSelect v-model="config.server" :items="serverOptions" label="Emby 服务器" /></VCol>
          <VCol cols="12" md="7"><VSelect v-model="config.libraries" :items="libraryOptions" label="电影库" multiple chips closable-chips /></VCol>
          <VCol cols="12" sm="6" md="3"><VSwitch v-model="config.enabled" label="启用插件" color="primary" /></VCol>
          <VCol cols="12" sm="6" md="3"><VSwitch v-model="config.show_sidebar_nav" label="显示侧栏入口" color="primary" /></VCol>
          <VCol cols="12" sm="6" md="3"><VSwitch v-model="config.overwrite_images" label="更新已有图片" color="primary" /></VCol>
          <VCol cols="12" sm="6" md="3"><VSwitch v-model="config.sync_logo" label="同步合集徽标" color="primary" /></VCol>
        </VRow>
        <div class="d-flex flex-wrap ga-3 mt-2">
          <VBtn prepend-icon="mdi-content-save" color="primary" variant="tonal" :loading="saving" @click="emit('save')">保存设置</VBtn>
          <VBtn prepend-icon="mdi-file-search-outline" color="primary" :disabled="job.running" @click="emit('scan')">生成预演</VBtn>
        </div>
      </VCardText>
    </VCard>

    <VTabs v-model="tab" class="mb-3">
      <VTab value="plan">变更计划</VTab>
      <VTab value="exceptions">异常项目 {{ plan.anomalies?.length || 0 }}</VTab>
    </VTabs>

    <VWindow v-model="tab">
      <VWindowItem value="plan">
        <VRow class="mb-2">
          <VCol v-for="item in [
            ['电影', summary.movies || 0], ['合集', summary.collections || 0], ['新建', summary.create || 0],
            ['待接管', summary.adopt || 0], ['加入', summary.add || 0], ['移除', summary.remove || 0]
          ]" :key="item[0]" cols="6" sm="4" md="2">
            <VCard variant="tonal"><VCardText><div class="text-caption">{{ item[0] }}</div><div class="text-h5">{{ item[1] }}</div></VCardText></VCard>
          </VCol>
        </VRow>

        <VAlert v-if="!rows.length" type="info" variant="tonal" text="尚未生成预演计划。保存服务器和电影库后点击“生成预演”。" />
        <template v-else>
          <div class="d-flex flex-wrap align-center ga-3 mb-3">
            <VBtn size="small" variant="text" @click="selectAllChanges">选择全部变更</VBtn>
            <VBtn size="small" variant="text" @click="selected = []">清空选择</VBtn>
            <VSpacer />
            <VBtn color="error" prepend-icon="mdi-play-circle-outline" :disabled="!selected.length || job.running" @click="requestApply">执行所选 {{ selected.length }}</VBtn>
          </div>
          <VExpansionPanels multiple>
            <VExpansionPanel v-for="row in rows" :key="row.key">
              <VExpansionPanelTitle>
                <VCheckboxBtn v-model="selected" :value="String(row.key)" class="me-3" @click.stop />
                <div class="d-flex align-center ga-3 flex-grow-1">
                  <VAvatar rounded size="54" color="surface-variant"><VImg v-if="row.poster" :src="row.poster" cover /><VIcon v-else icon="mdi-movie-filter" /></VAvatar>
                  <div>
                    <div class="font-weight-medium">{{ row.name }}</div>
                    <div class="text-caption text-medium-emphasis">TMDB {{ row.tmdb_id }} · 目标 {{ row.desired_movies?.length || 0 }} 部</div>
                  </div>
                  <VSpacer />
                  <VChip v-if="row.create" size="small" color="primary">新建</VChip>
                  <VChip v-if="row.requires_adoption" size="small" color="warning">待接管</VChip>
                  <VChip v-if="row.add?.length" size="small" color="success">+{{ row.add.length }}</VChip>
                  <VChip v-if="row.remove?.length" size="small" color="error">-{{ row.remove.length }}</VChip>
                </div>
              </VExpansionPanelTitle>
              <VExpansionPanelText>
                <VAlert v-if="row.requires_adoption" type="warning" variant="tonal" class="mb-3">
                  检测到同名合集“{{ row.candidate_name }}”。只有勾选接管后，插件才会校正其成员和图片。
                  <VCheckbox v-model="adopted" :value="String(row.key)" label="确认接管此合集" hide-details />
                </VAlert>
                <VRow>
                  <VCol cols="12" md="4"><div class="text-subtitle-2 mb-2">加入成员</div><div v-for="item in row.add || []" :key="item.id" class="text-body-2">{{ item.name }}</div><span v-if="!row.add?.length" class="text-medium-emphasis">无</span></VCol>
                  <VCol cols="12" md="4"><div class="text-subtitle-2 mb-2">移除错误成员</div><div v-for="item in row.remove || []" :key="item.Id || item.id" class="text-body-2">{{ item.Name || item.name }}</div><span v-if="!row.remove?.length" class="text-medium-emphasis">无</span></VCol>
                  <VCol cols="12" md="4"><div class="text-subtitle-2 mb-2">图片</div><div class="d-flex ga-4"><div><VImg v-if="row.poster" :src="row.poster" width="90" aspect-ratio="0.667" cover class="rounded" /><div class="text-caption">封面 {{ row.poster_language || '-' }}</div></div><div><VImg v-if="row.logo" :src="row.logo" width="140" height="90" contain /><div class="text-caption">徽标 {{ row.logo_language || '-' }}</div></div></div></VCol>
                </VRow>
              </VExpansionPanelText>
            </VExpansionPanel>
          </VExpansionPanels>
        </template>
      </VWindowItem>
      <VWindowItem value="exceptions">
        <VList v-if="plan.anomalies?.length" lines="two">
          <VListItem v-for="item in plan.anomalies" :key="item.id" :title="item.name" :subtitle="`${item.reason} · Emby ${item.id}`" />
        </VList>
        <VAlert v-else type="success" variant="tonal" text="没有异常项目。" />
      </VWindowItem>
    </VWindow>

    <VDialog v-model="confirmDialog" max-width="520">
      <VCard>
        <VCardTitle>确认执行合集变更</VCardTitle>
        <VCardText>将执行 {{ selected.length }} 个合集计划，可能创建合集、加入或移除成员，并更新封面和徽标。电影文件不会被删除。是否继续？</VCardText>
        <VCardActions><VSpacer /><VBtn @click="confirmDialog = false">取消</VBtn><VBtn color="error" @click="confirmApply">确认执行</VBtn></VCardActions>
      </VCard>
    </VDialog>
  </div>
</template>

<style scoped>
.collection-manager { max-width: 1500px; margin: 0 auto; }
</style>
