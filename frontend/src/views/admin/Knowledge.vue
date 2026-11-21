<script setup>
// 管理端 - 知识库管理（文档 12.5.2）
// 文档列表 + 上传 + 重建索引 + 多模态/纯文本检索测试对比
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Upload, RefreshRight, RefreshLeft, Search, Plus } from '@element-plus/icons-vue'
import http, { mediaUrl } from '@/api/http'
import ImageAttachment from '@/components/ImageAttachment.vue'
import { formatBytes, formatDateTime, modalityLabel } from '@/utils/format'

/* ---------------- 文档列表 ---------------- */
const docs = ref([])
const loading = ref(false)

const categories = [
  { value: 'network', label: '网络' },
  { value: 'password', label: '密码' },
  { value: 'software', label: '软件' },
  { value: 'finance', label: '财务' },
  { value: 'email', label: '邮件' },
  { value: 'general', label: '通用' }
]
const categoryLabel = (v) => categories.find((c) => c.value === v)?.label || v || '-'

// 模态字段是 {text:n, image:n, table:n} 的计数对象（不是数组）；
// 展示时遍历键取模态名、值为分块数，仅保留 >0 的项
const hasModalities = (m) => Object.values(m || {}).some((n) => Number(n) > 0)

async function loadDocs() {
  loading.value = true
  try {
    const list = await http.get('/admin/knowledge')
    docs.value = Array.isArray(list) ? list : []
  } finally {
    loading.value = false
  }
}

/* ---------------- 上传文档 ---------------- */
const uploadVisible = ref(false)
const uploading = ref(false)
const uploadForm = reactive({ category: 'general', file: null })
const uploadFileRef = ref(null)

function openUpload() {
  uploadForm.category = 'general'
  uploadForm.file = null
  uploadVisible.value = true
}

function onFileChange(e) {
  uploadForm.file = e.target.files?.[0] || null
}

async function submitUpload() {
  if (!uploadForm.file) {
    ElMessage.warning('请选择要上传的文档')
    return
  }
  uploading.value = true
  try {
    const fd = new FormData()
    fd.append('file', uploadForm.file)
    fd.append('category', uploadForm.category)
    const res = await http.post('/admin/knowledge/upload', fd, {
      headers: { 'Content-Type': 'multipart/form-data' }
    })
    ElMessage.success(`上传成功，新增 ${res.chunk_count ?? 0} 个分块`)
    uploadVisible.value = false
    loadDocs()
  } finally {
    uploading.value = false
  }
}

/* ---------------- 查看 / 删除 ---------------- */
const detailVisible = ref(false)
const detailDoc = ref(null)
const chunks = ref([])
const chunkLoading = ref(false)

// 统一取图地址：优先后端给的 url，回退 asset_id 拼媒体接口，再经 mediaUrl 补 token
function itemImageUrl(item) {
  return mediaUrl(item.url || (item.asset_id ? `/api/media/${item.asset_id}` : ''))
}

async function viewDoc(row) {
  detailDoc.value = row
  detailVisible.value = true
  chunks.value = []
  chunkLoading.value = true
  try {
    const res = await http.get(`/admin/knowledge/${row.id}/chunks`)
    chunks.value = Array.isArray(res) ? res : []
  } finally {
    chunkLoading.value = false
  }
}

async function removeDoc(row) {
  try {
    await ElMessageBox.confirm(`确认删除文档「${row.filename}」？`, '提示', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning'
    })
  } catch {
    return
  }
  await http.delete(`/admin/knowledge/${row.id}`)
  ElMessage.success('已删除')
  loadDocs()
}

/* ---------------- 重建索引 / 加载演示库 ---------------- */
const rebuilding = ref(false)
async function rebuildIndex() {
  rebuilding.value = true
  try {
    const res = await http.post('/admin/knowledge/rebuild')
    ElMessage.success(`重建完成：文档 ${res.documents ?? 0} 个，分块 ${res.chunks ?? 0} 个`)
    loadDocs()
  } finally {
    rebuilding.value = false
  }
}

const seeding = ref(false)
async function seedKnowledge() {
  seeding.value = true
  try {
    const res = await http.post('/admin/knowledge/seed')
    const n = Array.isArray(res.created) ? res.created.length : 0
    ElMessage.success(`已加载演示知识库，新增 ${n} 个文档`)
    loadDocs()
  } finally {
    seeding.value = false
  }
}

/* ---------------- 检索测试 ---------------- */
const searchMode = ref('multimodal')
const searchQuery = ref('')
const searchResults = ref([])
const searching = ref(false)
const searchImage = ref(null) // 上传截图 { url, assetId }
const searchImageRef = ref(null)

function triggerSearchImage() {
  searchImageRef.value?.click()
}

async function onSearchImageChange(e) {
  const file = e.target.files?.[0]
  e.target.value = ''
  if (!file) return
  const fd = new FormData()
  fd.append('file', file)
  const res = await http.post('/media/upload', fd, {
    headers: { 'Content-Type': 'multipart/form-data' }
  })
  searchImage.value = { url: res.url, assetId: res.asset_id }
}

async function runSearch() {
  if (!searchQuery.value.trim() && !searchImage.value) {
    ElMessage.warning('请输入问题或上传截图')
    return
  }
  searching.value = true
  try {
    const body = {
      query: searchQuery.value.trim(),
      mode: searchMode.value
    }
    // 若上传了截图，附带图片地址供多模态检索（后端契约扩展字段）
    if (searchImage.value) body.image_url = searchImage.value.url
    const res = await http.post('/admin/knowledge/test_search', body)
    searchResults.value = Array.isArray(res) ? res : []
  } finally {
    searching.value = false
  }
}

// 重置检索条件与结果，恢复默认模式
function resetSearch() {
  searchMode.value = 'multimodal'
  searchQuery.value = ''
  searchImage.value = null
  searchResults.value = []
}

onMounted(loadDocs)
</script>

<template>
  <div class="knowledge-page">
    <!-- 文档列表 -->
    <el-card shadow="never" class="section-card">
      <template #header>
        <div class="card-header-row">
          <span class="card-title">知识库文档</span>
          <div class="header-actions">
            <el-button size="small" :icon="Plus" @click="openUpload">上传文档</el-button>
            <el-button
              size="small"
              :icon="RefreshRight"
              :loading="rebuilding"
              @click="rebuildIndex"
            >
              重建索引
            </el-button>
            <el-button size="small" :loading="seeding" @click="seedKnowledge">
              加载演示知识库
            </el-button>
          </div>
        </div>
      </template>

      <el-table v-loading="loading" :data="docs" size="small" empty-text="暂无文档">
        <el-table-column prop="filename" label="文件名" min-width="180" show-overflow-tooltip />
        <el-table-column label="分类" width="100">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ categoryLabel(row.category) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="模态" width="160">
          <template #default="{ row }">
            <el-tag
              v-for="(count, name) in row.modalities || {}"
              v-show="count > 0"
              :key="name"
              size="small"
              effect="plain"
              class="mod-tag"
            >
              {{ modalityLabel(name) }} ×{{ count }}
            </el-tag>
            <span v-if="!hasModalities(row.modalities)">-</span>
          </template>
        </el-table-column>
        <el-table-column prop="chunk_count" label="分块数" width="90" />
        <el-table-column label="大小" width="100">
          <template #default="{ row }">{{ formatBytes(row.size_bytes) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag
              size="small"
              :type="row.status === 'ready' || !row.status ? 'success' : 'info'"
            >
              {{ row.status || 'ready' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="上传时间" width="170">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="140" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="viewDoc(row)">查看</el-button>
            <el-button link type="danger" size="small" @click="removeDoc(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 检索测试 -->
    <el-card shadow="never" class="section-card">
      <template #header><span class="card-title">测试检索</span></template>

      <div class="search-form">
        <div class="search-row">
          <span class="label">检索模式：</span>
          <el-radio-group v-model="searchMode" size="small">
            <el-radio-button value="multimodal">多模态</el-radio-button>
            <el-radio-button value="text_only">纯文本</el-radio-button>
          </el-radio-group>
        </div>

        <div class="search-row">
          <el-input
            v-model="searchQuery"
            placeholder="输入问题，例如：VPN连不上怎么办"
            style="flex: 1 1 320px; min-width: 220px; max-width: 460px"
            clearable
            @keyup.enter="runSearch"
          />
          <el-button :icon="Upload" @click="triggerSearchImage">上传截图</el-button>
          <el-button type="primary" :icon="Search" :loading="searching" @click="runSearch">
            检索
          </el-button>
          <el-button :icon="RefreshLeft" @click="resetSearch">重置</el-button>
        </div>

        <div v-if="searchImage" class="search-image-preview">
          <el-image :src="mediaUrl(searchImage.url)" fit="cover" class="preview-img" />
          <el-button link type="danger" size="small" @click="searchImage = null">
            移除截图
          </el-button>
        </div>

        <div class="search-tip">
          提示：切换「纯文本」时图像块与表格块不参与召回，可对比多模态检索的价值。
        </div>
      </div>

      <el-divider />

      <div class="result-list">
        <div class="result-title">检索结果（{{ searchResults.length }}）</div>
        <el-empty
          v-if="!searchResults.length"
          description="暂无结果，请执行检索"
          :image-size="60"
        />
        <div v-for="(item, idx) in searchResults" :key="idx" class="result-item">
          <div class="result-head">
            <span class="result-index">{{ idx + 1 }}</span>
            <span class="result-source">{{ item.source || '未知来源' }}</span>
            <span v-if="item.page" class="result-page">第 {{ item.page }} 页</span>
            <el-tag size="small" effect="plain">{{ modalityLabel(item.modality) }}</el-tag>
            <el-tag size="small" type="success" effect="plain">
              相似度 {{ Number(item.score ?? 0).toFixed(3) }}
            </el-tag>
          </div>
          <div class="result-content">{{ item.content }}</div>
          <div v-if="itemImageUrl(item)" class="result-image">
            <ImageAttachment :src="itemImageUrl(item)" :source="item.source" />
          </div>
        </div>
      </div>
    </el-card>

    <!-- 上传对话框 -->
    <el-dialog v-model="uploadVisible" title="上传文档" width="480px">
      <el-form label-width="80px">
        <el-form-item label="分类">
          <el-select v-model="uploadForm.category" style="width: 100%">
            <el-option
              v-for="c in categories"
              :key="c.value"
              :label="c.label"
              :value="c.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="文件">
          <input ref="uploadFileRef" type="file" @change="onFileChange" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="uploadVisible = false">取消</el-button>
        <el-button type="primary" :loading="uploading" @click="submitUpload">上传</el-button>
      </template>
    </el-dialog>

    <!-- 文档详情 -->
    <el-dialog v-model="detailVisible" title="文档详情" width="720px">
      <el-descriptions v-if="detailDoc" :column="1" border size="small">
        <el-descriptions-item label="文件名">{{ detailDoc.filename }}</el-descriptions-item>
        <el-descriptions-item label="分类">{{ categoryLabel(detailDoc.category) }}</el-descriptions-item>
        <el-descriptions-item label="模态">
          <el-tag
            v-for="(count, name) in detailDoc.modalities || {}"
            v-show="count > 0"
            :key="name"
            size="small"
            effect="plain"
            class="mod-tag"
          >
            {{ modalityLabel(name) }} ×{{ count }}
          </el-tag>
          <span v-if="!hasModalities(detailDoc.modalities)">-</span>
        </el-descriptions-item>
        <el-descriptions-item label="分块数">{{ detailDoc.chunk_count }}</el-descriptions-item>
        <el-descriptions-item label="大小">{{ formatBytes(detailDoc.size_bytes) }}</el-descriptions-item>
        <el-descriptions-item label="状态">{{ detailDoc.status || 'ready' }}</el-descriptions-item>
        <el-descriptions-item label="上传时间">{{ formatDateTime(detailDoc.created_at) }}</el-descriptions-item>
      </el-descriptions>

      <div class="chunk-section">
        <div class="chunk-header">
          <span class="chunk-title">分片内容</span>
          <span class="chunk-count">共 {{ chunks.length }} 个分片</span>
        </div>
        <div v-loading="chunkLoading" class="chunk-list">
          <el-empty
            v-if="!chunkLoading && !chunks.length"
            description="暂无分片信息"
            :image-size="60"
          />
          <div v-for="c in chunks" :key="c.chunk_index" class="chunk-item">
            <div class="chunk-head">
              <span class="chunk-index">#{{ c.chunk_index }}</span>
              <el-tag size="small" effect="plain">{{ modalityLabel(c.modality) }}</el-tag>
              <span v-if="c.page" class="chunk-page">第 {{ c.page }} 页</span>
            </div>
            <div v-if="c.modality === 'image'" class="chunk-image">
              <el-image
                :src="itemImageUrl(c)"
                fit="contain"
                class="chunk-img"
                :preview-src-list="[itemImageUrl(c)]"
                preview-teleported
              />
              <div v-if="c.content" class="chunk-content chunk-context">{{ c.content }}</div>
            </div>
            <div v-else class="chunk-content">{{ c.content }}</div>
          </div>
        </div>
      </div>
    </el-dialog>

    <input
      ref="searchImageRef"
      type="file"
      accept="image/*"
      style="display: none"
      @change="onSearchImageChange"
    />
  </div>
</template>

<style scoped>
.knowledge-page {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 16px;
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

.header-actions {
  display: flex;
  gap: 8px;
}

.mod-tag {
  margin-right: 4px;
}

.search-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.search-row .label {
  font-size: 14px;
  color: var(--mde-text-regular);
}

.search-image-preview {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
}

.preview-img {
  width: 80px;
  height: 80px;
  border-radius: 6px;
  border: 1px solid var(--mde-border);
}

.search-tip {
  font-size: 12px;
  color: var(--mde-text-secondary);
}

.result-title {
  font-weight: 600;
  margin-bottom: 10px;
}

.result-item {
  border: 1px solid var(--mde-border-light);
  border-radius: var(--mde-radius-sm);
  padding: 10px 12px;
  margin-bottom: 10px;
}

.result-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
}

.result-index {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--mde-primary-light);
  color: var(--mde-primary);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 600;
}

.result-source {
  font-weight: 600;
}

.result-page {
  font-size: 12px;
  color: var(--mde-text-secondary);
}

.result-content {
  font-size: 13px;
  line-height: 1.6;
  color: var(--mde-text-regular);
  margin-bottom: 8px;
  white-space: pre-wrap;
}

.chunk-section {
  margin-top: 16px;
}

.chunk-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--mde-border-light);
  margin-bottom: 10px;
}

.chunk-title {
  font-weight: 600;
  font-size: 14px;
}

.chunk-count {
  font-size: 12px;
  color: var(--mde-text-secondary);
}

.chunk-list {
  max-height: 360px;
  overflow-y: auto;
}

.chunk-item {
  border: 1px solid var(--mde-border-light);
  border-radius: var(--mde-radius-sm);
  padding: 8px 10px;
  margin-bottom: 8px;
}

.chunk-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.chunk-index {
  font-size: 12px;
  font-weight: 600;
  color: var(--mde-primary);
}

.chunk-page {
  font-size: 12px;
  color: var(--mde-text-secondary);
}

.chunk-content {
  font-size: 13px;
  line-height: 1.6;
  color: var(--mde-text-regular);
  white-space: pre-wrap;
  word-break: break-word;
}

.chunk-context {
  margin-top: 6px;
  color: var(--mde-text-secondary);
}

.chunk-img {
  max-width: 100%;
  max-height: 180px;
  border-radius: 4px;
  border: 1px solid var(--mde-border);
}
</style>
