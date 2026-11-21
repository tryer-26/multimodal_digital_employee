<script setup>
// 参考来源列表：默认折叠为“参考来源 (N)”，点击展开
import { ref, computed } from 'vue'
import { modalityLabel } from '@/utils/format'

const props = defineProps({
  citations: { type: Array, default: () => [] }
})

const expanded = ref(false)

const count = computed(() => props.citations.length)

// 模态对应的标签色
function tagType(modality) {
  return { text: 'primary', image: 'success', table: 'warning', voice: 'info' }[modality] || 'info'
}
</script>

<template>
  <div v-if="count" class="citation-list">
    <div class="citation-head" @click="expanded = !expanded">
      <el-icon class="caret" :class="{ open: expanded }"><CaretRight /></el-icon>
      <span>参考来源 ({{ count }})</span>
    </div>
    <transition name="fade">
      <ul v-show="expanded" class="citation-items">
        <li v-for="(c, idx) in citations" :key="idx" class="citation-item">
          <span class="idx">{{ idx + 1 }}.</span>
          <span class="source">{{ c.source || '未知来源' }}</span>
          <span v-if="c.page" class="page">第 {{ c.page }} 页</span>
          <el-tag :type="tagType(c.modality)" size="small" effect="plain">
            {{ modalityLabel(c.modality) }}
          </el-tag>
        </li>
      </ul>
    </transition>
  </div>
</template>

<style scoped>
.citation-list {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--mde-border);
  font-size: 12px;
}

.citation-head {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  color: var(--mde-text-secondary);
  user-select: none;
}

.citation-head:hover {
  color: var(--mde-primary);
}

.caret {
  transition: transform 0.2s;
}

.caret.open {
  transform: rotate(90deg);
}

.citation-items {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
}

.citation-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 0;
  color: var(--mde-text-regular);
}

.idx {
  color: var(--mde-text-secondary);
}

.source {
  font-weight: 500;
}

.page {
  color: var(--mde-text-secondary);
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.2s;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
