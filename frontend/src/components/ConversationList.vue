<script setup>
// 历史会话列表（用户端左侧栏）
// 展示 title + 最近活跃相对时间 + 已结束标记，按最近活跃倒序；悬停可删除
import { computed } from 'vue'
import { Delete, Plus, Tickets } from '@element-plus/icons-vue'
import { relativeTime } from '@/utils/format'

const props = defineProps({
  conversations: { type: Array, default: () => [] },
  activeId: { type: [String, Number], default: '' },
  loading: { type: Boolean, default: false }
})

const emit = defineEmits(['select', 'new', 'tickets', 'delete'])

// 按 last_message_at 倒序（后端一般已排序，这里再兜底一次）
const sortedList = computed(() =>
  [...props.conversations].sort((a, b) => {
    const ta = new Date(a.last_message_at || a.started_at || 0).getTime()
    const tb = new Date(b.last_message_at || b.started_at || 0).getTime()
    return tb - ta
  })
)

function isClosed(c) {
  return c.status === 'closed'
}
</script>

<template>
  <div class="conv-list">
    <div class="conv-actions">
      <el-button type="primary" class="full-btn" :icon="Plus" @click="emit('new')">
        新会话
      </el-button>
      <el-button class="full-btn" :icon="Tickets" @click="emit('tickets')">
        我的工单
      </el-button>
    </div>

    <div v-loading="loading" class="conv-scroll">
      <div
        v-for="c in sortedList"
        :key="c.id"
        class="conv-item"
        :class="{ active: String(c.id) === String(activeId) }"
        @click="emit('select', c)"
      >
        <div class="conv-body">
          <div class="conv-title mde-ellipsis">
            {{ c.title || '新会话' }}
          </div>
          <div class="conv-sub">
            <span class="time">{{ relativeTime(c.last_message_at || c.started_at) }}</span>
            <span v-if="isClosed(c)" class="closed">已结束</span>
          </div>
        </div>
        <el-button
          class="conv-del"
          text
          size="small"
          :icon="Delete"
          title="删除会话"
          @click.stop="emit('delete', c)"
        />
      </div>

      <el-empty
        v-if="!sortedList.length && !loading"
        description="暂无历史会话"
        :image-size="60"
      />
    </div>
  </div>
</template>

<style scoped>
.conv-list {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.conv-actions {
  display: flex;
  gap: 8px;
  padding: 12px;
  border-bottom: 1px solid var(--mde-border);
  flex-shrink: 0;
}

.full-btn {
  flex: 1;
}

.conv-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.conv-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 12px;
  border-radius: var(--mde-radius-sm);
  cursor: pointer;
  margin-bottom: 4px;
  transition: background 0.15s;
}

.conv-body {
  flex: 1;
  min-width: 0;
}

.conv-del {
  flex-shrink: 0;
  opacity: 0;
  transition: opacity 0.15s;
}

.conv-item:hover .conv-del,
.conv-item.active .conv-del {
  opacity: 1;
}

.conv-del:hover {
  color: var(--mde-danger);
}

.conv-item:hover {
  background: var(--mde-bg-page);
}

.conv-item.active {
  background: var(--mde-primary-light);
}

.conv-title {
  font-size: 14px;
  color: var(--mde-text-primary);
  margin-bottom: 4px;
}

.conv-sub {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--mde-text-secondary);
}

.closed {
  color: var(--mde-text-placeholder);
}
</style>
