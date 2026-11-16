<template>
  <section class="page" data-module="runtime">
    <header class="page-head">
      <div>
        <h2>运行实例管理</h2>
        <p class="page-desc">
          记录每次ETL作业的运行实例，并回查该运行在基线健康时间线中的运行水位、缺失分区与预计完成时间。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出运行实例清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>基线结论</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in rows"
          :key="String(row.id)"
          :class="{ selected: selectedId === row.id }"
          @click="selectRow(row)"
        >
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span class="pill" :class="riskClass(row['基线风险'])">{{ row['基线风险'] || '未纳入' }}</span>
            {{ row['基线结论'] || '未纳入基线时间线' }}
          </td>
          <td class="row-actions" @click.stop>
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无运行实例数据</td>
        </tr>
      </tbody>
    </table>

    <section v-if="timeline" class="panel">
      <header class="panel-head">
        <h4>基线时间线回查 · {{ timeline.runtime?.['记录编号'] }} {{ timeline.runtime?.['作业名称'] }}</h4>
        <button class="link" type="button" @click="timeline = null">收起</button>
      </header>
      <p class="hint">{{ timeline.message }}</p>
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">运行水位</span>
          <strong class="stat-value">{{ timeline.water ?? '—' }}%</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">缺失分区</span>
          <strong class="stat-value">{{ timeline.missing ?? 0 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">预计完成时间</span>
          <strong class="stat-value small">{{ timeline.eta || '—' }}</strong>
        </article>
      </div>
      <p v-if="timeline.missing_partitions" class="hint">缺失分区明细：{{ timeline.missing_partitions }}</p>

      <table class="data-table compact">
        <thead>
          <tr><th>基线</th><th>版本</th><th>水位</th><th>缺失分区</th><th>承诺完成</th><th>预警时间</th><th>预计完成</th><th>健康状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in timeline.attributions" :key="item.基线编号 + item.版本号">
            <td>{{ item.基线编号 }}</td>
            <td>v{{ item.版本号 }}</td>
            <td>{{ item.运行水位 }}%</td>
            <td>{{ item.缺失分区 }} 个</td>
            <td>{{ item.承诺完成时间 || '—' }}</td>
            <td>{{ item.预警时间 || '—' }}</td>
            <td>{{ item.预计完成时间 || '—' }}</td>
            <td>{{ item.健康状态 }}</td>
          </tr>
          <tr v-if="!timeline.attributions.length">
            <td colspan="8" class="empty-state">该运行尚未归属任何基线</td>
          </tr>
        </tbody>
      </table>

      <ol class="node-list">
        <li v-for="node in timeline.nodes" :key="node.节点键" class="node-item" :class="riskClass(node.风险级别)">
          <span class="node-time">{{ node.节点时间 || '—' }}</span>
          <span class="pill" :class="riskClass(node.风险级别)">{{ node.节点类型 }}</span>
          <div>
            <strong>{{ node.标题 }}</strong>
            <p>{{ node.详情 }}</p>
            <span class="node-meta">{{ node.基线编号 }} · 交易号 {{ node.交易号 }}</span>
          </div>
        </li>
      </ol>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条运行实例记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/runtime'
const columns = ['记录编号', '作业名称', '开始时间', '结束时间', '执行状态', '读取行数', '写入行数', '错误信息']
const actions = ['启动作业', '终止作业', '标记成功']
const stats = [{"label": "运行记录总数", "value": 0}, {"label": "今日运行数", "value": 0}, {"label": "失败记录数", "value": 0}]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const selectedId = ref<number | null>(null)
const timeline = ref<any>(null)

const computedStats = computed(() => {
  const failed = rows.value.filter((row) => String(row['执行状态'] ?? '').includes('失败')).length
  const abnormal = rows.value.filter((row) => row.abnormal).length
  return [
    { label: '运行记录总数', value: total.value },
    { label: '基线高风险', value: rows.value.filter((row) => row['基线风险'] === '高').length },
    { label: '异常记录数', value: failed + abnormal },
  ]
})

const shownStats = computed(() => (rows.value.length ? computedStats.value : stats))

function riskClass(value: unknown): string {
  const text = String(value ?? '')
  if (text === '已破线' || text === '高') return 'high'
  if (text === '预警中' || text === '中') return 'mid'
  if (text === '正常' || text === '低') return 'low'
  return 'idle'
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function selectRow(row: Row) {
  selectedId.value = Number(row.id)
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/baseline-timeline`)
    if (!response.ok) {
      throw new Error('基线时间线回查失败')
    }
    timeline.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '基线时间线回查失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('运行实例动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行实例操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('运行记录列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    if (timeline.value && selectedId.value !== null) {
      await selectRow({ id: selectedId.value })
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行实例列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
tr.selected { background: #eff6ff; }
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-top: 14px; }
.panel-head { display: flex; justify-content: space-between; align-items: center; }
.panel-head h4 { margin: 0 0 6px; font-size: 14px; }
.hint { font-size: 12px; color: var(--muted); margin: 0 0 8px; }
.stat-value.small { font-size: 16px; }
.data-table.compact th, .data-table.compact td { padding: 6px 8px; font-size: 12px; }
.pill { display: inline-block; border-radius: 999px; padding: 1px 8px; font-size: 12px; background: #e2e8f0; color: #334155; }
.pill.high { background: #fee4e2; color: #b42318; }
.pill.mid { background: #fef0c7; color: #b54708; }
.pill.low { background: #dcfae6; color: #067647; }
.node-list { list-style: none; margin: 8px 0 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.node-item { display: grid; grid-template-columns: 120px auto 1fr; gap: 8px; border-left: 3px solid #cbd5e1; padding: 6px 8px; background: #fbfcfe; border-radius: 0 6px 6px 0; }
.node-item.high { border-left-color: #b42318; }
.node-item.mid { border-left-color: #d97706; }
.node-time { font-size: 12px; color: var(--muted); }
.node-item p { margin: 2px 0; font-size: 12px; }
.node-meta { font-size: 11px; color: var(--muted); }
</style>
