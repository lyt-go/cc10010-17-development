<template>
  <section class="page" data-module="execution">
    <header class="page-head">
      <div>
        <h2>作业执行水位</h2>
        <p class="page-desc">
          作业执行组件读取基线健康时间线，只取执行期需要的运行水位、缺失分区和预计完成时间。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="reload">刷新时间线数据</button>
      </div>
    </header>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>作业名称</span>
        <input v-model="job" placeholder="按作业名称过滤" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="reset">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th>基线编号</th><th>基线名称</th><th>版本</th><th>关联作业</th><th>运行编号</th>
          <th>承诺完成时间</th><th>预警时间</th><th>运行水位</th><th>缺失分区</th>
          <th>预计完成时间</th><th>破线风险</th><th>观测时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in items" :key="row.基线编号 + row.运行编号 + row.关联作业">
          <td>{{ row.基线编号 }}</td>
          <td>{{ row.基线名称 }}</td>
          <td>v{{ row.版本号 }}</td>
          <td>{{ row.关联作业 }}</td>
          <td>{{ row.运行编号 }}</td>
          <td>{{ row.承诺完成时间 || '—' }}</td>
          <td>{{ row.预警时间 || '—' }}</td>
          <td>{{ row.运行水位 ?? '—' }}%</td>
          <td>{{ row.缺失分区 ?? 0 }}</td>
          <td>{{ row.预计完成时间 || '—' }}</td>
          <td><span class="pill" :class="riskClass(row.破线风险)">{{ row.破线风险 }}</span></td>
          <td>{{ row.观测时间 || '—' }}</td>
        </tr>
        <tr v-if="!items.length">
          <td colspan="12" class="empty-state">没有匹配的基线时间线数据</td>
        </tr>
      </tbody>
    </table>

    <p v-if="loadedAt" class="hint">数据取自基线健康时间线，生成于 {{ loadedAt }}</p>
    <p v-if="errorMessage" class="error-text">{{ errorMessage }}</p>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type ExecutionRow = Record<string, any>

const job = ref('')
const items = ref<ExecutionRow[]>([])
const loadedAt = ref('')
const errorMessage = ref('')

function riskClass(value: unknown): string {
  const text = String(value ?? '')
  if (text === '高') return 'high'
  if (text === '中') return 'mid'
  if (text === '低') return 'low'
  return 'idle'
}

function reset() {
  job.value = ''
  void reload()
}

async function reload() {
  errorMessage.value = ''
  const query = job.value.trim() ? `?job=${encodeURIComponent(job.value.trim())}` : ''
  try {
    const response = await request(`/api/baseline/timeline/execution${query}`)
    if (!response.ok) {
      throw new Error('基线时间线读取失败')
    }
    const payload = await response.json()
    items.value = payload.items ?? []
    loadedAt.value = payload.generated_at ?? ''
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '基线时间线读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.hint { font-size: 12px; color: var(--muted); margin-top: 8px; }
.pill { display: inline-block; border-radius: 999px; padding: 1px 8px; font-size: 12px; background: #e2e8f0; color: #334155; }
.pill.high { background: #fee4e2; color: #b42318; }
.pill.mid { background: #fef0c7; color: #b54708; }
.pill.low { background: #dcfae6; color: #067647; }
</style>
