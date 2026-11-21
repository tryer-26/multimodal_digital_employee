<script setup>
// 管理端 - 工具调用日志（文档 12.5.6）
// 工具调用表格（分页）+ 操作列（查看详情 / 删除）
// 详情抽屉展示输入参数 / 输出结果 JSON（美化）
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import http from '@/api/http'
import { formatDateTime, formatDuration, prettyJson } from '@/utils/format'

const logs = ref([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)

async function loadLogs() {
  loading.value = true
  try {
    const res = await http.get('/admin/tools', {
      params: { page: page.value, page_size: pageSize.value }
    })
    logs.value = res.items || []
    total.value = res.total || 0
  } finally {
    loading.value = false
  }
}

function handleSearch() {
  page.value = 1
  loadLogs()
}

/* ---------------- 详情 ---------------- */
const detailVisible = ref(false)
const detailRow = ref(null)

function openDetail(row) {
  detailRow.value = row
  detailVisible.value = true
}

/* ---------------- 删除 ---------------- */
async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(
      `确认删除工具调用记录 #${row.id}（${row.tool_name}）？该操作不可恢复。`,
      '删除工具调用日志',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return // 取消
  }
  try {
    await http.delete(`/admin/tools/${row.id}`)
    ElMessage.success('工具调用记录已删除')
    if (detailRow.value?.id === row.id) detailVisible.value = false
    if (logs.value.length === 1 && page.value > 1) page.value -= 1
    loadLogs()
  } catch {
    // 错误提示由 http 拦截器统一处理
  }
}

onMounted(loadLogs)
</script>

<template>
  <div class="tools-page">
    <el-card shadow="never" class="section-card">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">工具调用日志</span>
          <el-button size="small" @click="loadLogs">刷新</el-button>
        </div>
      </template>

      <el-table
        v-loading="loading"
        :data="logs"
        size="small"
        empty-text="暂无工具调用"
        highlight-current-row
      >
        <el-table-column label="时间" width="180">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column prop="conversation_id" label="会话ID" width="180" show-overflow-tooltip />
        <el-table-column prop="tool_name" label="工具名" min-width="160" />
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.status === 'success' || row.status === 'ok' ? 'success' : 'danger'"
            >
              {{ row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="耗时" width="120">
          <template #default="{ row }">{{ formatDuration(row.latency_ms) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <div class="row-actions">
              <el-button link type="primary" size="small" @click="openDetail(row)">查看</el-button>
              <el-button link type="danger" size="small" @click="handleDelete(row)">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>

      <div class="pager">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          background
          @current-change="loadLogs"
          @size-change="handleSearch"
        />
      </div>
    </el-card>

    <!-- 详情抽屉 -->
    <el-drawer v-model="detailVisible" title="工具调用详情" size="55%">
      <template v-if="detailRow">
        <el-descriptions :column="2" border size="small" class="desc">
          <el-descriptions-item label="工具名">{{ detailRow.tool_name }}</el-descriptions-item>
          <el-descriptions-item label="状态">{{ detailRow.status }}</el-descriptions-item>
          <el-descriptions-item label="会话ID">{{ detailRow.conversation_id }}</el-descriptions-item>
          <el-descriptions-item label="耗时">{{ formatDuration(detailRow.latency_ms) }}</el-descriptions-item>
          <el-descriptions-item label="调用时间" :span="2">
            {{ formatDateTime(detailRow.created_at) }}
          </el-descriptions-item>
        </el-descriptions>

        <div class="json-block">
          <div class="json-title">输入参数</div>
          <pre class="json-pre">{{ prettyJson(detailRow.input_params) }}</pre>
        </div>

        <div class="json-block">
          <div class="json-title">输出结果</div>
          <pre class="json-pre">{{ prettyJson(detailRow.output_result) }}</pre>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.tools-page {
  padding: 16px;
}

.section-card {
  border: 1px solid var(--mde-border);
}

.card-header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.card-title {
  font-weight: 600;
  font-size: 15px;
}

.row-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.pager {
  display: flex;
  justify-content: flex-end;
  margin-top: 14px;
}

.desc {
  margin-bottom: 16px;
}

.json-block {
  margin-bottom: 16px;
}

.json-title {
  font-weight: 600;
  font-size: 14px;
  margin-bottom: 8px;
}

.json-pre {
  background: #1f2d3d;
  color: #d4e0ec;
  padding: 12px 14px;
  border-radius: var(--mde-radius-sm);
  font-size: 12px;
  line-height: 1.6;
  overflow: auto;
  max-height: 320px;
  margin: 0;
  white-space: pre-wrap;
  word-break: break-all;
  font-family: 'Consolas', 'Monaco', monospace;
}
</style>
