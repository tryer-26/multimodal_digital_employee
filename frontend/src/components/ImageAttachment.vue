<script setup>
// 图片附件缩略图：点击使用 el-image 放大预览
// 图片地址使用后端返回的 url（如 /api/media/xxx），由 Vite 代理到后端
import { computed } from 'vue'

const props = defineProps({
  // 图片地址
  src: { type: String, default: '' },
  // 来源说明（命中知识库来源文件名等）
  source: { type: String, default: '' },
  // 页码
  page: { type: [Number, String], default: null },
  // 缩略图宽度
  width: { type: Number, default: 140 }
})

// 预览列表（单击放大）
const previewList = computed(() => (props.src ? [props.src] : []))

const caption = computed(() => {
  if (!props.source) return ''
  return props.page ? `${props.source} · 第${props.page}页` : props.source
})
</script>

<template>
  <div v-if="src" class="image-attachment">
    <el-image
      :src="src"
      :preview-src-list="previewList"
      :initial-index="0"
      fit="cover"
      :style="{ width: width + 'px', height: width + 'px' }"
      preview-teleported
      hide-on-click-modal
      class="thumb"
    >
      <template #error>
        <div class="img-error">
          <el-icon><Picture /></el-icon>
          <span>图片加载失败</span>
        </div>
      </template>
    </el-image>
    <div v-if="caption" class="img-caption">{{ caption }}</div>
  </div>
</template>

<style scoped>
.image-attachment {
  display: inline-flex;
  flex-direction: column;
  gap: 4px;
  max-width: 100%;
}

.thumb {
  border-radius: var(--mde-radius-sm);
  border: 1px solid var(--mde-border);
  cursor: zoom-in;
  overflow: hidden;
}

.img-error {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  color: var(--mde-text-secondary);
  font-size: 12px;
  background: var(--mde-bg-page);
}

.img-caption {
  font-size: 12px;
  color: var(--mde-text-secondary);
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
