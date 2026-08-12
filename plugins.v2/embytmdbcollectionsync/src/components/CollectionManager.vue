<script setup>
import { computed, ref, watch } from 'vue'

const props = defineProps({
  status: { type: Object, default: () => ({}) },
  config: { type: Object, default: () => ({}) },
  loading: Boolean,
  saving: Boolean,
  hideTitle: Boolean,
})
const emit = defineEmits(['refresh', 'save', 'scan', 'apply', 'subscribe', 'restore', 'collection-action', 'cancel'])

const tab = ref('plan')
const confirmDialog = ref(false)
const detailsDialog = ref(false)
const detailsRow = ref(null)
const actionDialog = ref(false)
const pendingAction = ref(null)
const selected = ref([])
const adopted = ref([])
const plan = computed(() => props.status?.plan || {})
const rows = computed(() => plan.value?.collections || [])
const summary = computed(() => plan.value?.summary || {})
const deferred = computed(() => plan.value?.deferred || [])
const ignored = computed(() => props.status?.ignored || [])
const job = computed(() => props.status?.job || {})
const busy = computed(() => Boolean(job.value.running || job.value.busy))
const planIdentity = computed(() => plan.value?.plan_id || plan.value?.created_at || '')
const serverLibraries = computed(() => (props.status?.libraries || []).filter(item => !props.config.server || item.server === props.config.server))
const libraryOptions = computed(() => serverLibraries.value.map(item => ({ title: `${item.name}（${item.count || 0}）`, value: item.id })))
const serverOptions = computed(() => props.status?.servers || [])
const stats = computed(() => [
  ['电影', summary.value.movies || 0], ['合集', summary.value.collections || 0], ['新建', summary.value.create || 0],
  ['待接管', summary.value.adopt || 0], ['加入', summary.value.add || 0], ['移除', summary.value.remove || 0],
  ['缺片', summary.value.missing || 0], ['手工保护', summary.value.customized || 0],
  ['暂缓', summary.value.deferred || 0], ['已忽略', summary.value.ignored || 0],
])

function resetSelection() {
  selected.value = []
  adopted.value = []
  confirmDialog.value = false
  detailsDialog.value = false
  detailsRow.value = null
  actionDialog.value = false
  pendingAction.value = null
}

// 新预演计划不能沿用旧计划的勾选和接管状态。
watch(planIdentity, resetSelection, { immediate: true })
watch(rows, value => {
  const keys = new Set(value.map(row => String(row.key)))
  selected.value = selected.value.filter(key => keys.has(String(key)))
  adopted.value = adopted.value.filter(key => keys.has(String(key)))
}, { deep: true })

function rowHasChanges(row) {
  return !row.customized && Boolean(row.create || row.add?.length || row.remove?.length || row.poster || row.logo)
}

function subscribeMissing(row) {
  if (busy.value || !row.missing_movies?.length) return
  emit('subscribe', {
    plan_id: plan.value?.plan_id || '',
    tmdb_ids: row.missing_movies.map(item => String(item.tmdb_id)),
  })
}

function restoreManagement(row) {
  if (busy.value || !row.customized) return
  emit('restore', { collection_id: String(row.key) })
}

const actionCopy = {
  ignore: { title: '忽略这个 TMDB 合集', text: '以后扫描到它也不会再创建或提出接管。你可以在“已忽略”中恢复。', confirm: '确认忽略', color: 'warning' },
  mark_custom: { title: '确认为手工合集', text: '插件会锁定当前 Emby 合集及其全部成员，后续不再移动、移除或重复分配这些电影。', confirm: '确认锁定', color: 'primary' },
  delete_ignore: { title: '删除并忽略合集', text: '将删除这个插件管理的 Emby 合集，但不会删除其中的电影文件；同时永久忽略对应 TMDB 合集，避免以后重建。', confirm: '删除并忽略', color: 'error' },
}

function requestCollectionAction(row, action) {
  pendingAction.value = { row, action }
  actionDialog.value = true
}

function confirmCollectionAction() {
  if (!pendingAction.value) return
  const { row, action } = pendingAction.value
  actionDialog.value = false
  emit('collection-action', {
    action,
    collection_id: String(row.key),
    plan_id: plan.value?.plan_id || '',
  })
  pendingAction.value = null
}

function restoreIgnored(item) {
  emit('collection-action', { action: 'restore_ignore', collection_id: String(item.key) })
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
          插件会识别 TMDB 合集缺片并支持一键订阅。合集 Poster 和 Logo 使用 TMDB，均排除繁体候选；已接管合集若被用户手工改名或调整成员，会自动转为“人工合集保护”。
        </VAlert>
        <div class="d-flex flex-wrap ga-3 mt-3">
          <VBtn prepend-icon="mdi-content-save" color="primary" variant="tonal" :loading="saving" @click="emit('save')">保存设置</VBtn>
          <VBtn prepend-icon="mdi-file-search-outline" color="primary" :disabled="busy" @click="emit('scan')">生成预演</VBtn>
        </div>
      </VCardText>
    </VCard>

    <VTabs v-model="tab" class="mb-3">
      <VTab value="plan">变更计划</VTab>
      <VTab value="deferred">暂缓 {{ deferred.length }}</VTab>
      <VTab value="ignored">已忽略 {{ ignored.length }}</VTab>
      <VTab value="exceptions">异常项目 {{ plan.anomalies?.length || 0 }}</VTab>
    </VTabs>

    <VWindow v-model="tab">
      <VWindowItem value="plan">
        <div class="stats-strip mb-5" aria-label="预演汇总">
          <div v-for="item in stats" :key="item[0]" class="stat-cell">
            <span class="stat-label">{{ item[0] }}</span><strong class="stat-value">{{ item[1] }}</strong>
          </div>
        </div>

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
                <VImg v-if="row.poster" :src="row.poster" :alt="`${row.name} 海报`" cover aspect-ratio="0.667" />
                <div v-else class="collection-card__placeholder" role="img" :aria-label="`${row.name} 无海报`"><VIcon icon="mdi-movie-filter-outline" size="48" /><span>暂无 TMDB 海报</span></div>
                <div class="collection-card__overlay">
                  <VCheckboxBtn v-model="selected" :value="String(row.key)" color="primary" :disabled="row.customized" :aria-label="`选择 ${row.name}`" @click.stop />
                  <VChip v-if="row.create" size="small" color="primary" variant="flat">新建</VChip>
                  <VChip v-else-if="row.requires_adoption" size="small" color="warning" variant="flat">待接管</VChip>
                  <VChip v-else-if="row.customized" size="small" color="purple" variant="flat">人工保护</VChip>
                </div>
              </div>
              <VCardText class="collection-card__body">
                <div class="collection-card__title" :title="row.name">{{ row.name }}</div>
                <div class="collection-card__meta">TMDB {{ row.tmdb_id }} · {{ row.desired_movies?.length || 0 }} 部</div>
                <div class="d-flex flex-wrap ga-1 mt-2" aria-label="变更数量">
                  <VChip v-if="row.add?.length" size="small" color="success" variant="tonal">加入 {{ row.add.length }}</VChip>
                  <VChip v-if="row.remove?.length" size="small" color="error" variant="tonal">移除 {{ row.remove.length }}</VChip>
                  <VChip v-if="row.poster || row.logo" size="small" color="info" variant="tonal">图片</VChip>
                  <VChip v-if="row.missing_movies?.length" size="small" color="warning" variant="tonal">缺片 {{ row.missing_movies.length }}</VChip>
                  <VChip v-if="!rowHasChanges(row)" size="small" variant="tonal">无成员变更</VChip>
                </div>
                <div v-if="row.add?.length || row.remove?.length" class="collection-card__preview text-caption mt-2">
                  <div v-for="item in (row.add || []).slice(0, 1)" :key="`add-${item.id}`" class="text-success text-truncate">＋ {{ item.name }}</div>
                  <div v-for="item in (row.remove || []).slice(0, 1)" :key="`remove-${item.Id || item.id}`" class="text-error text-truncate">－ {{ item.Name || item.name }}</div>
                </div>
                <div v-if="row.requires_adoption" class="collection-card__notice mt-2 text-warning text-truncate">同名：{{ row.candidate_name }}</div>
                <div v-if="row.customized" class="collection-card__notice mt-2 text-info text-truncate">已锁定：手工合集优先</div>
              </VCardText>
              <VCardActions class="pt-0">
                <VBtn size="small" variant="text" icon="mdi-eye-outline" aria-label="查看详情" @click="openDetails(row)" />
                <VSpacer />
                <VBtn v-if="row.missing_movies?.length" size="small" color="primary" variant="tonal" icon="mdi-bell-plus-outline" aria-label="订阅缺片" :disabled="busy" @click="subscribeMissing(row)" />
                <VCheckboxBtn v-if="row.requires_adoption" v-model="adopted" :value="String(row.key)" color="warning" aria-label="确认接管" @click.stop />
                <VMenu location="bottom end">
                  <template #activator="{ props: menuProps }"><VBtn v-bind="menuProps" size="small" variant="text" icon="mdi-dots-vertical" aria-label="合集操作" /></template>
                  <VList density="compact" min-width="220">
                    <VListItem prepend-icon="mdi-eye-outline" title="查看详情" @click="openDetails(row)" />
                    <VListItem v-if="row.missing_movies?.length" prepend-icon="mdi-bell-plus-outline" title="订阅全部缺片" @click="subscribeMissing(row)" />
                    <VListItem v-if="!row.customized && (row.emby_id || row.candidate_emby_id)" prepend-icon="mdi-lock-check-outline" title="确认为手工合集" @click="requestCollectionAction(row, 'mark_custom')" />
                    <VListItem v-if="row.customized" prepend-icon="mdi-lock-open-outline" title="恢复 TMDB 管理" @click="restoreManagement(row)" />
                    <VDivider />
                    <VListItem v-if="!row.managed" prepend-icon="mdi-eye-off-outline" title="忽略，不再生成" base-color="warning" @click="requestCollectionAction(row, 'ignore')" />
                    <VListItem v-if="row.managed" prepend-icon="mdi-delete-forever-outline" title="删除并忽略" base-color="error" @click="requestCollectionAction(row, 'delete_ignore')" />
                  </VList>
                </VMenu>
              </VCardActions>
            </VCard>
          </div>
        </template>
      </VWindowItem>
      <VWindowItem value="deferred">
        <VAlert class="mb-4" type="info" variant="tonal">这些合集现在不会创建。当 TMDB 至少已有两部常规电影上映，或第二部电影入库后，插件会自动重新评估。</VAlert>
        <VList v-if="deferred.length" lines="three" class="rounded-lg" border>
          <VListItem v-for="item in deferred" :key="item.key" :title="item.name" :subtitle="`${item.reason} · 已上映 ${item.released_count} · 未来或信息不完整 ${item.future_or_invalid_count}`">
            <template #prepend><VAvatar color="surface-variant"><VIcon icon="mdi-timer-sand" /></VAvatar></template>
          </VListItem>
        </VList>
        <VAlert v-else type="success" variant="tonal" text="没有因单片或未来影片而暂缓的合集。" />
      </VWindowItem>
      <VWindowItem value="ignored">
        <VAlert class="mb-4" type="info" variant="tonal">忽略记录保存在插件数据库中，MoviePilot 重启后仍然有效。</VAlert>
        <VList v-if="ignored.length" lines="two" class="rounded-lg" border>
          <VListItem v-for="item in ignored" :key="item.key" :title="item.name" :subtitle="`${item.reason || '用户明确忽略'} · TMDB ${item.tmdb_id || item.key} · ${item.ignored_at || ''}`">
            <template #prepend><VAvatar color="surface-variant"><VIcon icon="mdi-eye-off-outline" /></VAvatar></template>
            <template #append><VBtn size="small" variant="tonal" :disabled="busy" @click="restoreIgnored(item)">恢复</VBtn></template>
          </VListItem>
        </VList>
        <VAlert v-else type="info" variant="tonal" text="还没有永久忽略的 TMDB 合集。" />
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
              <VAlert v-if="detailsRow.customized" type="info" variant="tonal" class="mb-4">该合集已检测到人工修改并锁定。只有点击“恢复 TMDB 管理”后，插件才会重新生成成员校正计划。</VAlert>
              <VRow>
                <VCol cols="12" sm="6"><div class="text-subtitle-2 mb-2">加入成员（{{ detailsRow.add?.length || 0 }}）</div><VList v-if="detailsRow.add?.length" density="compact" lines="one"><VListItem v-for="item in detailsRow.add" :key="item.id" :title="item.name" :subtitle="`Emby ${item.id}`" /></VList><span v-else class="text-medium-emphasis">无</span></VCol>
                <VCol cols="12" sm="6"><div class="text-subtitle-2 mb-2">移除错误成员（{{ detailsRow.remove?.length || 0 }}）</div><VList v-if="detailsRow.remove?.length" density="compact" lines="one"><VListItem v-for="item in detailsRow.remove" :key="item.Id || item.id" :title="item.Name || item.name" :subtitle="`Emby ${item.Id || item.id}`" /></VList><span v-else class="text-medium-emphasis">无</span></VCol>
              </VRow>
              <div v-if="detailsRow.missing_movies?.length" class="mt-4">
                <div class="d-flex align-center mb-2"><div class="text-subtitle-2">TMDB 合集缺片（{{ detailsRow.missing_movies.length }}）</div><VSpacer /><VBtn size="small" color="primary" prepend-icon="mdi-bell-plus-outline" :disabled="busy" @click="subscribeMissing(detailsRow)">一键订阅全部缺片</VBtn></div>
                <VList density="compact" lines="two"><VListItem v-for="item in detailsRow.missing_movies" :key="item.tmdb_id" :title="item.title" :subtitle="`${item.year || '年份未知'} · TMDB ${item.tmdb_id}`"><template #prepend><VAvatar rounded="0" size="44"><VImg v-if="item.poster" :src="item.poster" cover /><VIcon v-else icon="mdi-movie-outline" /></VAvatar></template></VListItem></VList>
              </div>
              <div v-if="detailsRow.logo" class="mt-4"><div class="text-subtitle-2 mb-2">徽标 {{ detailsRow.logo_language || '无' }}</div><VImg :src="detailsRow.logo" :alt="`${detailsRow.name} 徽标`" max-height="110" contain class="details-logo" /></div>
            </VCol>
          </VRow>
        </VCardText>
        <VCardActions>
          <VBtn v-if="!detailsRow.customized && (detailsRow.emby_id || detailsRow.candidate_emby_id)" color="primary" variant="tonal" prepend-icon="mdi-lock-check-outline" @click="requestCollectionAction(detailsRow, 'mark_custom')">确认为手工合集</VBtn>
          <VBtn v-if="detailsRow.customized" color="warning" variant="tonal" @click="restoreManagement(detailsRow)">恢复 TMDB 管理</VBtn>
          <VBtn v-if="!detailsRow.managed" color="warning" variant="text" @click="requestCollectionAction(detailsRow, 'ignore')">忽略此合集</VBtn>
          <VBtn v-if="detailsRow.managed" color="error" variant="text" @click="requestCollectionAction(detailsRow, 'delete_ignore')">删除并忽略</VBtn>
          <VSpacer /><VBtn @click="detailsDialog = false">关闭</VBtn>
        </VCardActions>
      </VCard>
    </VDialog>

    <VDialog v-model="actionDialog" max-width="520">
      <VCard v-if="pendingAction">
        <VCardTitle>{{ actionCopy[pendingAction.action]?.title }}</VCardTitle>
        <VCardText><strong>{{ pendingAction.row.name }}</strong><div class="mt-3 text-medium-emphasis">{{ actionCopy[pendingAction.action]?.text }}</div></VCardText>
        <VCardActions><VSpacer /><VBtn @click="actionDialog = false">取消</VBtn><VBtn :color="actionCopy[pendingAction.action]?.color" variant="flat" @click="confirmCollectionAction">{{ actionCopy[pendingAction.action]?.confirm }}</VBtn></VCardActions>
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
.stats-strip { display: grid; grid-template-columns: repeat(10, minmax(86px, 1fr)); overflow-x: auto; border: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); border-radius: 14px; background: rgb(var(--v-theme-surface)); }
.stat-cell { min-width: 86px; padding: 12px 14px; border-right: 1px solid rgba(var(--v-border-color), var(--v-border-opacity)); display: flex; flex-direction: column; gap: 3px; }
.stat-cell:last-child { border-right: 0; }
.stat-label { color: rgba(var(--v-theme-on-surface), .58); font-size: .75rem; line-height: 1; white-space: nowrap; }
.stat-value { color: rgb(var(--v-theme-on-surface)); font-size: 1.45rem; line-height: 1.1; font-variant-numeric: tabular-nums; white-space: nowrap; }
.collection-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 14px; }
.collection-card { min-width: 0; display: flex; flex-direction: column; overflow: hidden; border-radius: 12px; }
.collection-card__media { position: relative; background: rgb(var(--v-theme-surface-variant)); }
.collection-card__overlay { position: absolute; inset: 0 0 auto 0; display: flex; align-items: center; justify-content: space-between; padding: 6px 8px; background: linear-gradient(180deg, rgba(0, 0, 0, .58), transparent); }
.collection-card__placeholder { aspect-ratio: 2 / 3; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; color: rgba(var(--v-theme-on-surface), .58); }
.collection-card__body { flex: 1; min-width: 0; padding: 12px 12px 6px; }
.collection-card__title { min-height: 2.55em; overflow: hidden; display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 2; font-size: .9rem; line-height: 1.28; font-weight: 650; }
.collection-card__meta { color: rgba(var(--v-theme-on-surface), .55); font-size: .7rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.collection-card__preview { min-height: 18px; line-height: 1.35; }
.collection-card__notice { font-size: .7rem; }
.collection-card :deep(.v-card-actions) { min-height: 42px; padding: 4px 6px 7px; }
.details-placeholder { aspect-ratio: .667; display: grid; place-items: center; background: rgb(var(--v-theme-surface-variant)); color: rgba(var(--v-theme-on-surface), .58); border-radius: 8px; }
.details-logo { background: rgb(var(--v-theme-surface-variant)); border-radius: 8px; }
@media (max-width: 1400px) { .collection-grid { grid-template-columns: repeat(5, minmax(0, 1fr)); } }
@media (max-width: 1120px) { .collection-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); } }
@media (max-width: 860px) { .collection-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 580px) { .collection-manager { padding-inline: 12px !important; } .collection-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; } .collection-card__body { padding-inline: 9px; } }
</style>
