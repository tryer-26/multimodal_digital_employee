<script setup>
// 管理端 - 全栈评测（ASR / TTS / VLM / 检索）
// 左：历史运行列表；右：发起评测 + 运行进度 + 分维度报告（指标/基线/Δ/阈值/判定）+ 逐条明细
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete } from '@element-plus/icons-vue'
import http from '@/api/http'
import { formatDateTime, relativeTime } from '@/utils/format'

const suites = ref([])
const selected = ref([])
const maxCases = ref(0)

const runs = ref([])
const loadingRuns = ref(false)
const starting = ref(false)

const current = ref(null)
const baseline = ref(null)
const busy = ref(false)

let pollTimer = null

/* ---------------- 指标展示元数据 ---------------- */
const METRIC_LABELS = {
  cer: '字错率 CER',
  sentence_accuracy: '句准率',
  failure_rate: '识别失败率',
  synth_failed: '合成失败条数',
  latency_p50_ms: '时延 P50 (ms)',
  latency_p95_ms: '时延 P95 (ms)',
  measured: '有效样本数',
  success_rate: '合成成功率',
  round_trip_cer: '反向 ASR CER',
  mean_chars_per_sec: '平均语速 (字/秒)',
  duration_len_corr: '时长-文本长度相关性',
  truncation_rate: '疑似截断率',
  error_code_accuracy: '错误码准确率',
  service_hit_rate: '服务命中率',
  error_type_accuracy: '错误类型准确率',
  diagnosis_field_accuracy: '诊断字段总准确率',
  caption_cosine: '描述语义相似度',
  caption_keypoint_hit: '关键要素命中率',
  hallucination_free_rate: '无幻觉率',
  judge_available: 'judge 有效样本',
  'mm.recall@1': '多模态 Recall@1',
  'mm.recall@3': '多模态 Recall@3',
  'mm.mrr': '多模态 MRR',
  'mm.ndcg@3': '多模态 nDCG@3',
  'mm.hit@1': '多模态 Hit@1',
  'text_only.recall@1': '纯文本 Recall@1',
  'text_only.recall@3': '纯文本 Recall@3',
  'text_only.mrr': '纯文本 MRR',
  'text_only.ndcg@3': '纯文本 nDCG@3',
  'text_only.hit@1': '纯文本 Hit@1',
  'multimodal_gain@3': '多模态增益@3'
}

const LOWER_IS_BETTER = new Set([
  'cer',
  'round_trip_cer',
  'failure_rate',
  'truncation_rate',
  'latency_p50_ms',
  'latency_p95_ms'
])

function metricLabel(key) {
  return METRIC_LABELS[key] || key
}

function isPercent(key) {
  return key.endsWith('_rate') || key.endsWith('_accuracy') || key === 'cer' || key === 'round_trip_cer'
}

function fmtValue(key, v) {
  if (v === null || v === undefined || v === '') return '-'
  if (typeof v !== 'number') return String(v)
  if (isPercent(key)) return `${(v * 100).toFixed(1)}%`
  return Number.isInteger(v) ? String(v) : v.toFixed(4)
}

function fmtDelta(key, d) {
  if (d === null || d === undefined) return '-'
  const sign = d > 0 ? '+' : ''
  if (isPercent(key)) return `${sign}${(d * 100).toFixed(1)}%`
  return Number.isInteger(d) ? `${sign}${d}` : `${sign}${d.toFixed(4)}`
}

function deltaClass(key, d) {
  if (d === null || d === undefined || Math.abs(d) < 1e-9) return 'delta-flat'
  const good = LOWER_IS_BETTER.has(key) ? d < 0 : d > 0
  return good ? 'delta-good' : 'delta-bad'
}

function verdictType(v) {
  if (v === 'PASS') return 'success'
  if (v === 'FAIL') return 'danger'
  return 'info'
}

function runStatusType(s) {
  if (s === 'done') return 'success'
  if (s === 'failed') return 'danger'
  return 'warning'
}

function runStatusLabel(s) {
  return { running: '运行中', done: '已完成', failed: '失败' }[s] || s
}

/* ---------------- 数据加载 ---------------- */
async function loadSuites() {
  const res = await http.get('/admin/eval/suites')
  suites.value = res.suites || []
  maxCases.value = res.max_cases || 0
  selected.value = suites.value.map((s) => s.name)
}

async function loadRuns() {
  loadingRuns.value = true
  try {
    runs.value = await http.get('/admin/eval/runs')
  } finally {
    loadingRuns.value = false
  }
}

async function openRun(id, silent = false) {
  if (!silent) busy.value = true
  try {
    const res = await http.get(`/admin/eval/runs/${id}`)
    current.value = res.run
    baseline.value = res.baseline
    return res.run
  } finally {
    if (!silent) busy.value = false
  }
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function startPolling(id) {
  stopPolling()
  pollTimer = setInterval(async () => {
    try {
      const run = await openRun(id, true)
      if (run?.status !== 'running') {
        stopPolling()
        loadRuns()
      }
    } catch {
      stopPolling()
    }
  }, 2000)
}

async function startEval() {
  if (!selected.value.length) {
    ElMessage.warning('请至少选择一个评测套件')
    return
  }
  starting.value = true
  try {
    const res = await http.post('/admin/eval/run', { suites: selected.value })
    ElMessage.success('已开始评测，完成后自动刷新')
    await loadRuns()
    await openRun(res.id)
    startPolling(res.id)
  } finally {
    starting.value = false
  }
}

async function setBaseline() {
  if (!current.value) return
  await http.post(`/admin/eval/runs/${current.value.id}/baseline`)
  ElMessage.success('已设为基线')
  baseline.value = { run_id: current.value.id }
  await openRun(current.value.id, true)
}

async function removeRun(id) {
  const target = id || current.value?.id
  if (!target) return
  try {
    await ElMessageBox.confirm('确定删除该评测记录吗？', '提示', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning'
    })
  } catch {
    return
  }
  try {
    // 失败时由 http 响应拦截器统一提示（如“运行中的评测不可删除”）
    await http.delete(`/admin/eval/runs/${target}`)
  } catch {
    return
  }
  ElMessage.success('已删除')
  if (current.value?.id === target) current.value = null
  if (baseline.value?.run_id === target) baseline.value = null
  await loadRuns()
}

/* ---------------- 报告渲染 ---------------- */
const progressPercent = computed(() => {
  const p = current.value?.progress
  if (!p || !p.total) return 0
  return Math.round((p.done / p.total) * 100)
})

function criteriaMap(res) {
  const map = {}
  for (const c of res.criteria || []) map[c.metric] = c.threshold
  return map
}

function metricRows(res) {
  const thresholds = criteriaMap(res)
  const base = baseline.value?.results?.[res.suite] || {}
  return Object.entries(res.metrics || {}).map(([key, value]) => {
    const baseValue = base[key]
    const hasBase = typeof baseValue === 'number' && typeof value === 'number'
    return {
      key,
      label: metricLabel(key),
      value,
      base: baseValue === undefined ? null : baseValue,
      delta: hasBase ? value - baseValue : null,
      threshold: thresholds[key] === undefined ? null : thresholds[key]
    }
  })
}

function verdictSummary(r) {
  const vals = Object.values(r.summary || {}).map((s) => s.verdict)
  if (!vals.length) return '—'
  const count = (v) => vals.filter((x) => x === v).length
  return `PASS ${count('PASS')} · FAIL ${count('FAIL')} · SKIP ${count('SKIP')}`
}

function itemColumns(items) {
  if (!items?.length) return []
  return Object.keys(items[0]).slice(0, 10)
}

function cellText(v) {
  if (v === null || v === undefined) return '-'
  if (typeof v === 'object') return JSON.stringify(v)
  const s = String(v)
  return s.length > 200 ? `${s.slice(0, 200)}…` : s
}

/* ---------------- 生命周期 ---------------- */
onMounted(async () => {
  await Promise.all([loadSuites(), loadRuns()])
  const running = runs.value.find((r) => r.status === 'running')
  if (running) {
    await openRun(running.id)
    startPolling(running.id)
  }
})

onBeforeUnmount(stopPolling)
</script>

<template>
  <div class="eval-page">
    <div class="toolbar">
      <h2 class="page-h">全栈评测</h2>
      <div class="toolbar-right">
        <el-checkbox-group v-model="selected" size="small">
          <el-checkbox-button v-for="s in suites" :key="s.name" :value="s.name">
            {{ s.title }}
          </el-checkbox-button>
        </el-checkbox-group>
        <el-button type="primary" size="small" :loading="starting" @click="startEval">
          开始评测
        </el-button>
        <el-button size="small" @click="loadRuns">刷新</el-button>
      </div>
    </div>
    <div class="hint">
      评测会真实调用 ASR / TTS / VLM / 嵌入接口，每个套件最多 {{ maxCases }} 条用例（小样本限流）。
    </div>

    <div class="layout">
      <el-card class="history" shadow="never">
        <template #header><span class="card-title">历史运行</span></template>
        <div v-loading="loadingRuns" class="history-body">
          <el-empty v-if="!runs.length" description="暂无评测记录" :image-size="70" />
          <div
            v-for="r in runs"
            :key="r.id"
            class="run-item"
            :class="{ active: current?.id === r.id }"
            @click="openRun(r.id)"
          >
            <div class="run-line">
              <span class="run-id">{{ r.id }}</span>
              <div class="run-tail">
                <el-tag :type="runStatusType(r.status)" size="small" effect="plain">
                  {{ runStatusLabel(r.status) }}
                </el-tag>
                <el-tooltip
                  v-if="r.status !== 'running'"
                  content="删除该记录"
                  placement="top"
                >
                  <el-icon class="run-del" @click.stop="removeRun(r.id)"><Delete /></el-icon>
                </el-tooltip>
              </div>
            </div>
            <div class="run-sub">{{ relativeTime(r.started_at) }} · {{ r.suites.join(' / ') }}</div>
            <div class="run-sub">{{ verdictSummary(r) }}</div>
          </div>
        </div>
      </el-card>

      <div v-loading="busy" class="detail">
        <el-empty v-if="!current" description="选择左侧一次运行查看报告" />

        <template v-else>
          <el-card v-if="current.status === 'running'" shadow="never" class="progress-card">
            <el-progress :percentage="progressPercent" :stroke-width="14" striped striped-flow />
            <div class="progress-text">
              正在执行：{{ current.progress?.current || '准备中' }}（{{ current.progress?.done }}/{{ current.progress?.total }}）
            </div>
          </el-card>

          <el-card shadow="never" class="summary-card">
            <template #header>
              <div class="card-header-row">
                <span class="card-title">运行概览</span>
                <div>
                  <el-button size="small" :disabled="current.status !== 'done'" @click="setBaseline">
                    设为基线
                  </el-button>
                  <el-button
                    size="small"
                    type="danger"
                    plain
                    :disabled="current.status === 'running'"
                    @click="removeRun()"
                  >
                    删除
                  </el-button>
                </div>
              </div>
            </template>
            <el-descriptions :column="3" size="small" border>
              <el-descriptions-item label="运行 ID">{{ current.id }}</el-descriptions-item>
              <el-descriptions-item label="状态">{{ runStatusLabel(current.status) }}</el-descriptions-item>
              <el-descriptions-item label="基线">{{ baseline?.run_id || '未设置' }}</el-descriptions-item>
              <el-descriptions-item label="开始">{{ formatDateTime(current.started_at) }}</el-descriptions-item>
              <el-descriptions-item label="完成">
                {{ current.finished_at ? formatDateTime(current.finished_at) : '-' }}
              </el-descriptions-item>
              <el-descriptions-item label="用例上限">{{ current.config?.max_cases }}</el-descriptions-item>
              <el-descriptions-item label="ASR / TTS">
                {{ current.config?.asr_provider }} / {{ current.config?.tts_provider }}（{{ current.config?.tts_voice }}）
              </el-descriptions-item>
              <el-descriptions-item label="VLM">{{ current.config?.vlm_model || '-' }}</el-descriptions-item>
              <el-descriptions-item label="嵌入 / 精排">
                {{ current.config?.embedding_model || '-' }} / {{ current.config?.reranker_model || '-' }}
              </el-descriptions-item>
            </el-descriptions>
          </el-card>

          <el-card
            v-for="(res, name) in current.results"
            :key="name"
            shadow="never"
            class="suite-card"
          >
            <template #header>
              <div class="card-header-row">
                <span class="card-title">{{ res.title || name }}</span>
                <el-tag :type="verdictType(res.verdict)" size="small">{{ res.verdict }}</el-tag>
              </div>
            </template>

            <div v-if="res.notes?.length" class="notes">
              <div v-for="(n, i) in res.notes" :key="i">· {{ n }}</div>
            </div>

            <el-table v-if="Object.keys(res.metrics || {}).length" :data="metricRows(res)" size="small">
              <el-table-column prop="label" label="指标" min-width="200" />
              <el-table-column label="本次" width="120">
                <template #default="{ row }">{{ fmtValue(row.key, row.value) }}</template>
              </el-table-column>
              <el-table-column label="基线" width="120">
                <template #default="{ row }">{{ fmtValue(row.key, row.base) }}</template>
              </el-table-column>
              <el-table-column label="Δ" width="110">
                <template #default="{ row }">
                  <span :class="deltaClass(row.key, row.delta)">{{ fmtDelta(row.key, row.delta) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="阈值" width="110">
                <template #default="{ row }">
                  {{ row.threshold === null ? '-' : fmtValue(row.key, row.threshold) }}
                </template>
              </el-table-column>
            </el-table>

            <el-collapse v-if="res.items?.length" class="detail-collapse">
              <el-collapse-item :title="`逐条明细（${res.items.length} 条）`">
                <el-table :data="res.items" size="small" max-height="380">
                  <el-table-column
                    v-for="c in itemColumns(res.items)"
                    :key="c"
                    :label="metricLabel(c)"
                    min-width="140"
                    show-overflow-tooltip
                  >
                    <template #default="{ row }">{{ cellText(row[c]) }}</template>
                  </el-table-column>
                </el-table>
              </el-collapse-item>
            </el-collapse>
          </el-card>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.eval-page {
  padding: 16px;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.page-h {
  margin: 0;
  font-size: 18px;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.hint {
  font-size: 12px;
  color: var(--mde-text-secondary);
  margin-bottom: 12px;
}

.layout {
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: 16px;
  align-items: start;
}

.history,
.summary-card,
.suite-card,
.progress-card {
  border: 1px solid var(--mde-border);
}

.history-body {
  max-height: calc(100vh - 200px);
  overflow: auto;
}

.run-item {
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  border: 1px solid transparent;
  margin-bottom: 8px;
}

.run-item:hover {
  background: rgba(47, 107, 255, 0.06);
}

.run-item.active {
  background: rgba(47, 107, 255, 0.1);
  border-color: var(--mde-primary);
}

.run-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.run-tail {
  display: flex;
  align-items: center;
  gap: 6px;
}

.run-del {
  font-size: 14px;
  color: var(--mde-text-secondary);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s, color 0.15s;
}

.run-item:hover .run-del {
  opacity: 1;
}

.run-del:hover {
  color: var(--mde-danger, #f56c6c);
}

.run-id {
  font-size: 13px;
  font-weight: 600;
}

.run-sub {
  font-size: 12px;
  color: var(--mde-text-secondary);
  margin-top: 4px;
}

.detail {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: 200px;
}

.progress-text {
  margin-top: 10px;
  font-size: 13px;
  color: var(--mde-text-secondary);
}

.card-header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.card-title {
  font-weight: 600;
  font-size: 14px;
}

.notes {
  font-size: 12px;
  color: var(--mde-text-secondary);
  margin-bottom: 10px;
  line-height: 1.7;
}

.detail-collapse {
  margin-top: 8px;
}

.delta-good {
  color: var(--mde-success);
  font-weight: 600;
}

.delta-bad {
  color: var(--mde-danger, #f56c6c);
  font-weight: 600;
}

.delta-flat {
  color: var(--mde-text-secondary);
}

@media (max-width: 1100px) {
  .layout {
    grid-template-columns: 1fr;
  }
}
</style>
