<script setup>
// 管理端 - 数据看板（文档 12.5.1）
// 四指标卡 + 对话量趋势 + 情绪分布 + 意图分布 + 工具调用次数 + 最近转人工会话
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'
import http from '@/api/http'
import { WSClient } from '@/api/ws'
import BaseChart from '@/components/BaseChart.vue'
import { formatDuration, formatClockTime } from '@/utils/format'

const loading = ref(false)
const days = ref(7)
const data = reactive({
  session_total: 0,
  resolution_rate: 0,
  handoff_rate: 0,
  avg_response_ms: 0,
  today_sessions: 0,
  trend: [],
  emotion_dist: [],
  intent_dist: [],
  tool_calls: [],
  recent_handoffs: []
})

let ws = null

/* ---------------- 数据处理 ---------------- */
// 比率可能是 0-1 小数或已是百分数，统一成百分号字符串
function rateText(v) {
  if (v === null || v === undefined) return '-'
  const n = Number(v)
  const pct = n <= 1 ? n * 100 : n
  return `${pct.toFixed(1)}%`
}

async function loadDashboard() {
  loading.value = true
  try {
    const res = await http.get('/admin/dashboard', { params: { days: days.value } })
    Object.assign(data, {
      session_total: res.session_total || 0,
      resolution_rate: res.resolution_rate || 0,
      handoff_rate: res.handoff_rate || 0,
      avg_response_ms: res.avg_response_ms || 0,
      today_sessions: res.today_sessions || 0,
      trend: res.trend || [],
      emotion_dist: res.emotion_dist || [],
      intent_dist: res.intent_dist || [],
      tool_calls: res.tool_calls || [],
      recent_handoffs: res.recent_handoffs || []
    })
  } finally {
    loading.value = false
  }
}

/* ---------------- 图表 option ---------------- */
const trendOption = computed(() => ({
  tooltip: { trigger: 'axis' },
  grid: { left: 40, right: 20, top: 30, bottom: 30 },
  xAxis: {
    type: 'category',
    boundaryGap: false,
    data: data.trend.map((t) => t.date)
  },
  yAxis: { type: 'value', minInterval: 1 },
  series: [
    {
      name: '对话量',
      type: 'line',
      smooth: true,
      areaStyle: { opacity: 0.15 },
      itemStyle: { color: '#2f6bff' },
      data: data.trend.map((t) => t.count)
    }
  ]
}))

const emotionOption = computed(() => {
  // 键名与后端 analytics 返回的 emotion_dist.name 一致（积极/中性/消极）
  const colors = { 积极: '#22a06b', 中性: '#909399', 消极: '#f56c6c' }
  return {
    tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
    legend: { bottom: 0 },
    series: [
      {
        name: '情绪分布',
        type: 'pie',
        radius: ['40%', '65%'],
        center: ['50%', '45%'],
        data: data.emotion_dist.map((e) => ({
          name: e.name,
          value: e.value,
          itemStyle: { color: colors[e.name] }
        }))
      }
    ]
  }
})

const intentOption = computed(() => ({
  tooltip: { trigger: 'axis' },
  grid: { left: 40, right: 20, top: 30, bottom: 40 },
  xAxis: {
    type: 'category',
    data: data.intent_dist.map((i) => i.name),
    axisLabel: { interval: 0, rotate: data.intent_dist.length > 5 ? 30 : 0 }
  },
  yAxis: { type: 'value', minInterval: 1 },
  series: [
    {
      name: '意图分布',
      type: 'bar',
      barMaxWidth: 40,
      itemStyle: { color: '#409eff', borderRadius: [4, 4, 0, 0] },
      data: data.intent_dist.map((i) => i.value)
    }
  ]
}))

const toolOption = computed(() => ({
  tooltip: { trigger: 'axis' },
  grid: { left: 40, right: 20, top: 30, bottom: 40 },
  xAxis: {
    type: 'category',
    data: data.tool_calls.map((t) => t.name),
    axisLabel: { interval: 0, rotate: data.tool_calls.length > 5 ? 30 : 0 }
  },
  yAxis: { type: 'value', minInterval: 1 },
  series: [
    {
      name: '调用次数',
      type: 'bar',
      barMaxWidth: 40,
      itemStyle: { color: '#e6a23c', borderRadius: [4, 4, 0, 0] },
      data: data.tool_calls.map((t) => t.value)
    }
  ]
}))

/* ---------------- 生命周期 ---------------- */
onMounted(() => {
  loadDashboard()
  // 管理端 WebSocket：收到 stats_update 即刷新看板
  ws = new WSClient('admin')
  ws.on('stats_update', () => loadDashboard())
  ws.connect()
})

onBeforeUnmount(() => {
  ws?.close()
})
</script>

<template>
  <div class="dashboard">
    <div class="toolbar">
      <h2 class="page-h">数据看板</h2>
      <div class="toolbar-right">
        <el-radio-group v-model="days" size="small" @change="loadDashboard">
          <el-radio-button :value="7">近7天</el-radio-button>
          <el-radio-button :value="14">近14天</el-radio-button>
          <el-radio-button :value="30">近30天</el-radio-button>
        </el-radio-group>
        <el-button size="small" @click="loadDashboard">刷新</el-button>
      </div>
    </div>

    <!-- 指标卡 -->
    <div v-loading="loading" class="metric-row">
      <div class="metric-card">
        <div class="metric-label">今日对话量</div>
        <div class="metric-value">{{ data.today_sessions }}</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">解决率</div>
        <div class="metric-value success">{{ rateText(data.resolution_rate) }}</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">转人工率</div>
        <div class="metric-value warning">{{ rateText(data.handoff_rate) }}</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">平均响应时间</div>
        <div class="metric-value primary">{{ formatDuration(data.avg_response_ms) }}</div>
      </div>
    </div>

    <!-- 图表区 -->
    <div class="chart-grid">
      <el-card shadow="never" class="chart-card">
        <template #header><span class="card-title">对话量趋势</span></template>
        <BaseChart :option="trendOption" :loading="loading" height="300px" />
      </el-card>

      <el-card shadow="never" class="chart-card">
        <template #header><span class="card-title">情绪分布</span></template>
        <BaseChart :option="emotionOption" :loading="loading" height="300px" />
      </el-card>

      <el-card shadow="never" class="chart-card">
        <template #header><span class="card-title">意图分布</span></template>
        <BaseChart :option="intentOption" :loading="loading" height="300px" />
      </el-card>

      <el-card shadow="never" class="chart-card">
        <template #header><span class="card-title">工具调用次数</span></template>
        <BaseChart :option="toolOption" :loading="loading" height="300px" />
      </el-card>
    </div>

    <!-- 最近转人工会话 -->
    <el-card shadow="never" class="table-card">
      <template #header><span class="card-title">最近转人工会话</span></template>
      <el-table :data="data.recent_handoffs" size="small" empty-text="暂无数据">
        <el-table-column label="时间" width="140">
          <template #default="{ row }">
            {{ row.time?.length > 8 ? formatClockTime(row.time) : row.time }}
          </template>
        </el-table-column>
        <el-table-column prop="user" label="用户" width="120" />
        <el-table-column prop="reason" label="转接原因" show-overflow-tooltip />
        <el-table-column prop="agent" label="坐席" width="120" />
        <el-table-column label="处理时长" width="140">
          <template #default="{ row }">
            {{ row.duration_seconds != null ? formatDuration(row.duration_seconds * 1000) : '-' }}
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.dashboard {
  padding: 16px;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.page-h {
  margin: 0;
  font-size: 18px;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.metric-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 16px;
}

.metric-card {
  background: var(--mde-bg-card);
  border-radius: var(--mde-radius);
  box-shadow: var(--mde-shadow);
  padding: 18px 20px;
}

.metric-label {
  font-size: 13px;
  color: var(--mde-text-secondary);
  margin-bottom: 10px;
}

.metric-value {
  font-size: 28px;
  font-weight: 700;
}

.metric-value.success {
  color: var(--mde-success);
}

.metric-value.warning {
  color: var(--mde-warning);
}

.metric-value.primary {
  color: var(--mde-primary);
}

.chart-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
  margin-bottom: 16px;
}

.chart-card,
.table-card {
  border: 1px solid var(--mde-border);
}

.card-title {
  font-weight: 600;
  font-size: 14px;
}

@media (max-width: 1100px) {
  .metric-row,
  .chart-grid {
    grid-template-columns: 1fr 1fr;
  }
}
</style>
