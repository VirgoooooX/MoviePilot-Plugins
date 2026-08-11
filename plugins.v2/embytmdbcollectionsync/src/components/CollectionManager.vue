<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({
  status: { type: Object, default: () => ({}) },
  config: { type: Object, default: () => ({}) },
  loading: Boolean,
  saving: Boolean,
  hideTitle: Boolean,
})
const emit = defineEmits(['refresh', 'save', 'scan', 'apply', 'cancel'])

const tab = ref('plan')
const confirmDialog = ref(false)
const detailsDialog = ref(false)
const detailsRow = ref(null)
const selected = ref([])
const adopted = ref([])
const plan = computed(() => props.status?.plan || {})
const rows = computed(() => plan.value?.collections || [])
const summary = computed(() => plan.value?.summary || {})
const job = computed(() => props.status?.job || {})
const busy = computed(() => Boolean(job.value.running || job.value.busy))
const planIdentity = computed(() => plan.value?.plan_id || plan.value?.created_at || '')
const serverLibraries = computed(() => (props.status?.libraries || []).filter(item => !props.config.server || item.server === props.config.server))
const libraryOptions = computed(() => serverLibraries.value.map(item => ({ title: `${item.name}（${item.count || 0}）`, value: item.id })))
const serverOptions = computed(() => props.status?.servers || [])

function resetSelection() {
  selected.value = []
  adopted.value = []
  confirmDialog.value = false
  detailsDialog.value = false
  detailsRow.value = null
}

// 新预演计划不能沿用旧计划的勾选和接管状态。
watch(planIdentity, resetSelection, { immediate: true })
watch(rows, value => {
  const keys = new Set(value.map(row => String(row.key)))
  selected.value = selected.value.filter(key => keys.has(String(key)))
  adopted.value = adopted.value.filter(key => keys.has(String(key)))
}, { deep: true })

function rowHasChanges(row) {
  return Boolean(row.create || row.add?.length || row.remove?.length || row.poster || row.logo)
}

// 选择所有存在实际变更的合集。
function selectAllChanges() {
  selected.value = rows.value.filter(rowHasChanges).map(row => String(row.key))
}

function clearSelection() {
  selected.value = []
  adopted.value = []
}

// 打开完整增删详情，卡片只展示短预览。
function openDetails(row) {
  detailsRow.value = row
  detailsDialog.value = true
}

// 打开高风险执行确认框。
function requestApply() {
  if (!selected.value.length || busy.value) return
  confirmDialog.value = true
}

// 提交审核过的合集计划。
function confirmApply() {
  confirmDialog.value = false
  const selectedKeys = new Set(selected.value.map(key => String(key)))
  emit('apply', {
    plan_id: plan.value?.plan_id || '',
    selected: selected.value,
    // 允许用户先勾接管再选卡片；提交时只发送实际选中的接管项。
    adopted: adopted.value.filter(key => selectedKeys.has(String(key))),
  })
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
    <VCard v-if="busy" variant="tonal" class="mb-5" aria-live="polite">
      <VCardText>
        <div class="d-flex justify-space-between mb-2">
          <span>{{ job.message || (job.cancel_requested ? '正在取消任务' : '后台任务运行中') }}</span>
          <span>{{ job.progress || 0 }}%</span>
        </div>
        <VProgressLinear :model-value="job.progress || 0" color="primary" rounded />
        <div class="text-caption text-medium-emphasis mt-2">
          阶段：{{ job.phase || '-' }}<span v-if="job.total"> · {{ job.current || 0 }}/{{ job.total }}</span><span v-if="job.run_id"> · {{ job.run_id }}</span>
        </div>
        <VBtn v-if="!job.cancel_requested" class="mt-3" size="small" variant="outlined" color="warning" prepend-icon="mdi-stop-circle-outline" @click="emit('cancel')">取消任务</VBtn>
        <VChip v-else class="mt-3" size="small" color="warning" variant="tonal">已请求取消，等待安全退出</VChip>
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
        <VAlert class="mt-1" type="info" variant="tonal" density="comfortable">
          合集封面和徽标只使用 TMDB 图片；地区标签兼容 zh-CN、zh-SG、zh-TW、zh-HK 及泛 zh，不再调用不支持合集 ID 的 Fanart movie 接口。
        </VAlert>
        <div class="d-flex flex-wrap ga-3 mt-3">
          <VBtn prepend-icon="mdi-content-save" color="primary" variant="tonal" :loading="saving" @click="emit('save')">保存设置</VBtn>
          <VBtn prepend-icon="mdi-file-search-outline" color="primary" :disabled="busy" @click="emit('scan')">生成预演</VBtn>
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
          <div class="d-flex flex-wrap align-center ga-3 mb-4">
            <VBtn size="small" variant="text" prepend-icon="mdi-check-all" @click="selectAllChanges">选择全部变更</VBtn>
            <VBtn size="small" variant="text" prepend-icon="mdi-close" @click="clearSelection">清空选择</VBtn>
            <VChip v-if="planIdentity" size="small" variant="tonal" color="primary">预演 {{ planIdentity }}</VChip>
            <VSpacer />
            <VBtn color="error" prepend-icon="mdi-play-circle-outline" :disabled="!selected.length || busy" @click="requestApply">执行所选 {{ selected.length }}</VBtn>
          </div>

          <div class="collection-grid" role="list" aria-label="TMDB 合集变更计划">
            <VCard v-for="row in rows" :key="row.key" class="collection-card" variant="outlined" role="listitem">
              <div class="collection-card__media">
                <VImg v-if="row.poster" :src="row.poster" :alt="`${row.name} 海报`" cover height="248" />
                <div v-else class="collection-card__placeholder" role="img" :aria-label="`${row.name} 无海报`"><VIcon icon="mdi-movie-filter-outline" size="48" /><span>暂无 TMDB 海报</span></div>
                <div class="collection-card__overlay">
                  <VCheckboxBtn v-model="selected" :value="String(row.key)" color="primary" :aria-label="`选择 ${row.name}`" @click.stop />
                  <VChip v-if="row.create" size="small" color="primary" variant="flat">新建</VChip>
                  <VChip v-else-if="row.requires_adoption" size="small" color="warning" variant="flat">待接管</VChip>
                </div>
              </div>
              <VCardText class="collection-card__body">
                <div class="collection-card__title" :title="row.name">{{ row.name }}</div>
                <div class="text-caption text-medium-emphasis">TMDB {{ row.tmdb_id }} · 目标 {{ row.desired_movies?.length || 0 }} 部</div>
                <div class="d-flex flex-wrap ga-1 mt-3" aria-label="变更数量">
                  <VChip v-if="row.add?.length" size="small" color="success" variant="tonal">加入 {{ row.add.length }}</VChip>
                  <VChip v-if="row.remove?.length" size="small" color="error" variant="tonal">移除 {{ row.remove.length }}</VChip>
                  <VChip v-if="row.poster || row.logo" size="small" color="info" variant="tonal">图片</VChip>
                  <VChip v-if="!rowHasChanges(row)" size="small" variant="tonal">无成员变更</VChip>
                </div>
                <div v-if="row.add?.length || row.remove?.length" class="collection-card__preview text-caption mt-3">
                  <div v-for="item in (row.add || []).slice(0, 2)" :key="`add-${item.id}`" class="text-success text-truncate">＋ {{ item.name }}</div>
                  <div v-for="item in (row.remove || []).slice(0, 2)" :key="`remove-${item.Id || item.id}`" class="text-error text-truncate">－ {{ item.Name || item.name }}</div>
                  <div v-if="(row.add?.length || 0) + (row.remove?.length || 0) > 4" class="text-medium-emphasis">还有更多成员，打开详情查看</div>
                </div>
                <VAlert v-if="row.requires_adoption" class="mt-3" type="warning" variant="tonal" density="compact">
                  同名合集“{{ row.candidate_name }}”
                </VAlert>
              </VCardText>
              <VCardActions class="pt-0">
                <VBtn size="small" variant="text" prepend-icon="mdi-eye-outline" @click="openDetails(row)">查看详情</VBtn>
                <VSpacer />
                <VCheckbox v-if="row.requires_adoption" v-model="adopted" :value="String(row.key)" label="确认接管" color="warning" hide-details density="compact" @click.stop />
              </VCardActions>
            </VCard>
          </div>
        </template>
      </VWindowItem>
      <VWindowItem value="exceptions">
        <VList v-if="plan.anomalies?.length" lines="two">
          <VListItem v-for="item in plan.anomalies" :key="item.id" :title="item.name" :subtitle="`${item.reason} · Emby ${item.id}`" />
        </VList>
        <VAlert v-else type="success" variant="tonal" text="没有异常项目。" />
      </VWindowItem>
    </VWindow>

    <VDialog v-model="detailsDialog" max-width="900" scrollable>
      <VCard v-if="detailsRow">
        <VCardTitle class="d-flex align-center ga-3"><span class="text-truncate">{{ detailsRow.name }}</span><VChip size="small" variant="tonal">TMDB {{ detailsRow.tmdb_id }}</VChip><VSpacer /><VBtn icon="mdi-close" variant="text" aria-label="关闭详情" @click="detailsDialog = false" /></VCardTitle>
        <VCardText>
          <VRow>
            <VCol cols="12" md="3"><VImg v-if="detailsRow.poster" :src="detailsRow.poster" :alt="`${detailsRow.name} 海报`" aspect-ratio="0.667" cover class="rounded" /><div v-else class="details-placeholder">暂无海报</div><div class="text-caption mt-2">封面 {{ detailsRow.poster_language || '无' }}</div></VCol>
            <VCol cols="12" md="9">
              <VAlert v-if="detailsRow.requires_adoption" type="warning" variant="tonal" class="mb-4">只有确认接管后，插件才会校正同名合集的成员与图片。</VAlert>
              <VRow>
                <VCol cols="12" sm="6"><div class="text-subtitle-2 mb-2">加入成员（{{ detailsRow.add?.length || 0 }}）</div><VList v-if="detailsRow.add?.length" density="compact" lines="one"><VListItem v-for="item in detailsRow.add" :key="item.id" :title="item.name" :subtitle="`Emby ${item.id}`" /></VList><span v-else class="text-medium-emphasis">无</span></VCol>
                <VCol cols="12" sm="6"><div class="text-subtitle-2 mb-2">移除错误成员（{{ detailsRow.remove?.length || 0 }}）</div><VList v-if="detailsRow.remove?.length" density="compact" lines="one"><VListItem v-for="item in detailsRow.remove" :key="item.Id || item.id" :title="item.Name || item.name" :subtitle="`Emby ${item.Id || item.id}`" /></VList><span v-else class="text-medium-emphasis">无</span></VCol>
              </VRow>
              <div v-if="detailsRow.logo" class="mt-4"><div class="text-subtitle-2 mb-2">徽标 {{ detailsRow.logo_language || '无' }}</div><VImg :src="detailsRow.logo" :alt="`${detailsRow.name} 徽标`" max-height="110" contain class="details-logo" /></div>
            </VCol>
          </VRow>
        </VCardText>
        <VCardActions><VSpacer /><VBtn @click="detailsDialog = false">关闭</VBtn></VCardActions>
      </VCard>
    </VDialog>

    <VDialog v-model="confirmDialog" max-width="520">
      <VCard>
        <VCardTitle>确认执行合集变更</VCardTitle>
        <VCardText>将执行 {{ selected.length }} 个合集计划。插件会在执行前重新读取成员快照，发现计划过期的合集会跳过并报告，不会覆盖电影文件。是否继续？</VCardText>
        <VCardActions><VSpacer /><VBtn @click="confirmDialog = false">取消</VBtn><VBtn color="error" @click="confirmApply">确认执行</VBtn></VCardActions>
      </VCard>
    </VDialog>
  </div>
</template>

<style scoped>
.collection-manager { max-width: 1500px; margin: 0 auto; }
.collection-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
.collection-card { min-width: 0; display: flex; flex-direction: column; overflow: hidden; }
.collection-card__media { position: relative; background: rgb(var(--v-theme-surface-variant)); }
.collection-card__overlay { position: absolute; inset: 0 0 auto 0; display: flex; align-items: center; justify-content: space-between; padding: 6px 8px; background: linear-gradient(180deg, rgba(0, 0, 0, .58), transparent); }
.collection-card__placeholder { height: 248px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; color: rgba(var(--v-theme-on-surface), .58); }
.collection-card__body { flex: 1; min-width: 0; }
.collection-card__title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
.collection-card__preview { min-height: 42px; line-height: 1.45; }
.details-placeholder { aspect-ratio: .667; display: grid; place-items: center; background: rgb(var(--v-theme-surface-variant)); color: rgba(var(--v-theme-on-surface), .58); border-radius: 8px; }
.details-logo { background: rgb(var(--v-theme-surface-variant)); border-radius: 8px; }
@media (max-width: 1100px) { .collection-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 620px) { .collection-grid { grid-template-columns: minmax(0, 1fr); } .collection-card__media :deep(.v-img) { height: 280px !important; } .collection-card__placeholder { height: 280px; } }
</style>
