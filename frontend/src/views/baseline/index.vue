<template>
  <section class="page" data-module="baseline">
    <header class="page-head">
      <div>
        <h2>基线管理</h2>
        <p class="page-desc">
          基线定义与运行分区数据关联成健康时间线，承诺完成时间、版本归属与破线风险在同一处可追溯。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出基线管理清单</button>
        <button class="btn" type="button" @click="recompute">重算时间线</button>
      </div>
    </header>

    <nav class="tab-row">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab"
        :class="{ active: activeTab === tab.key }"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </nav>

    <!-- 时间线视图：按承诺完成时间排列，节点按时间顺序展开 -->
    <template v-if="activeTab === 'timeline'">
      <div class="stat-row">
        <article v-for="item in overviewCards" :key="item.label" class="stat-card">
          <span class="stat-label">{{ item.label }}</span>
          <strong class="stat-value">{{ item.value }}</strong>
        </article>
      </div>

      <div class="timeline-layout">
        <ol class="baseline-rail">
          <li
            v-for="item in overviewBaselines"
            :key="item.基线编号"
            class="rail-item"
            :class="[riskClass(item.破线风险), { selected: selectedNo === item.基线编号 }]"
            @click="selectBaseline(item.基线编号)"
          >
            <span class="rail-time">{{ item.承诺完成时间 || '—' }}</span>
            <strong>{{ item.基线编号 }} {{ item.基线名称 }}</strong>
            <span class="rail-meta">
              {{ item.健康状态 }} · {{ item.破线风险 }}风险 · 水位 {{ item.运行水位 ?? '—' }}%
              · 缺失 {{ item.缺失分区数 ?? 0 }} 个分区
              <template v-if="item.归属版本"> · 归属 {{ item.归属版本 }}</template>
            </span>
            <span class="rail-desc">{{ item.基线结论 }}</span>
          </li>
          <li v-if="!overviewBaselines.length" class="empty-state">暂无基线时间线数据</li>
        </ol>

        <div class="timeline-detail">
          <div v-if="!detail" class="empty-state">选择左侧基线查看时间线节点</div>
          <template v-else>
            <header class="detail-head">
              <h3>{{ detail.baseline?.['基线编号'] }} {{ detail.baseline?.['基线名称'] }}</h3>
              <span class="detail-sub">
                当前版本 v{{ detail.state?.['当前版本'] }} · 归属 {{ detail.state?.['归属版本'] || '—' }}
                · 承诺完成时间 {{ detail.state?.['承诺完成时间'] || '—' }}
                · 预警时间 {{ detail.state?.['预警时间'] || '—' }}
              </span>
              <p class="detail-conclusion">{{ detail.state?.['基线结论'] }}</p>
            </header>

            <section class="panel">
              <h4>版本归属</h4>
              <table class="data-table compact">
                <thead>
                  <tr><th>版本</th><th>生效时间</th><th>承诺完成时间</th><th>预警余量</th><th>关联作业</th><th>承接</th><th>说明</th></tr>
                </thead>
                <tbody>
                  <tr v-for="version in detail.versions" :key="version.版本号">
                    <td>v{{ version.版本号 }}</td>
                    <td>{{ version.生效时间 || '—' }}</td>
                    <td>{{ version.承诺完成时间 || '—' }}</td>
                    <td>{{ version.预警余量分钟 }} 分钟</td>
                    <td>{{ (version.关联作业列表 || []).join('、') || '—' }}</td>
                    <td>{{ version.承接版本 ? 'v' + version.承接版本 : '基线定义' }}</td>
                    <td>{{ version.变更说明 || '—' }}</td>
                  </tr>
                </tbody>
              </table>
            </section>

            <section class="panel">
              <h4>作业归属明细</h4>
              <table class="data-table compact">
                <thead>
                  <tr>
                    <th>作业</th><th>运行编号</th><th>版本</th><th>选取依据</th>
                    <th>水位</th><th>缺失分区</th><th>预计完成</th><th>风险</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="item in detail.attributions" :key="item.运行编号 + item.作业名称">
                    <td>{{ item.作业名称 }}</td>
                    <td>{{ item.运行编号 }}</td>
                    <td>v{{ item.版本号 }}</td>
                    <td>{{ item.选取依据 }}</td>
                    <td>{{ item.运行水位 }}%</td>
                    <td>{{ item.缺失分区 }} 个</td>
                    <td>{{ item.预计完成时间 || '—' }}</td>
                    <td><span class="pill" :class="riskClass(item.风险级别)">{{ item.风险级别 }}</span></td>
                  </tr>
                  <tr v-if="!detail.attributions.length">
                    <td colspan="8" class="empty-state">该基线尚无归集的运行结果</td>
                  </tr>
                </tbody>
              </table>
            </section>

            <section class="panel">
              <h4>健康时间线（按时间排列）</h4>
              <ol class="node-list">
                <li v-for="node in detail.nodes" :key="node.节点键" class="node-item" :class="riskClass(node.风险级别)">
                  <span class="node-time">{{ node.节点时间 || '—' }}</span>
                  <span class="pill" :class="riskClass(node.风险级别)">{{ node.节点类型 }}</span>
                  <div class="node-body">
                    <strong>{{ node.标题 }}</strong>
                    <p>{{ node.详情 }}</p>
                    <span class="node-meta">
                      交易号 {{ node.交易号 }} · 归属依据 {{ node.归属依据 }}
                      <template v-if="node.运行编号"> · 运行 {{ node.运行编号 }}</template>
                    </span>
                  </div>
                </li>
                <li v-if="!detail.nodes.length" class="empty-state">暂无时间线节点</li>
              </ol>
            </section>
          </template>
        </div>
      </div>
    </template>

    <!-- 看板视图：按破线风险分栏 -->
    <template v-else-if="activeTab === 'board'">
      <div class="kanban">
        <section v-for="column in board.columns" :key="column.key" class="kanban-column">
          <header>
            <strong>{{ column.label }}</strong>
            <span>{{ column.items.length }}</span>
          </header>
          <article
            v-for="item in column.items"
            :key="item.基线编号"
            class="kanban-card"
            :class="riskClass(item.破线风险)"
          >
            <strong>{{ item.基线编号 }} {{ item.基线名称 }}</strong>
            <span>承诺 {{ item.承诺完成时间 || '—' }}</span>
            <span>预警 {{ item.预警时间 || '—' }} · 破线级别 {{ item.破线级别 || '—' }}</span>
            <span>水位 {{ item.运行水位 ?? '—' }}% · 缺失 {{ item.缺失分区数 ?? 0 }} 个分区</span>
            <span>预计完成 {{ item.预计完成时间 || '—' }}</span>
            <span class="kanban-version">当前 v{{ item.当前版本 }} · 归属 {{ item.归属版本 || '—' }}</span>
            <button class="link" type="button" @click="switchTab('timeline'); selectBaseline(item.基线编号)">
              查看时间线
            </button>
          </article>
          <p v-if="!column.items.length" class="empty-state">暂无</p>
        </section>
      </div>

      <section class="panel">
        <h4>最近破线 / 预警节点</h4>
        <table class="data-table compact">
          <thead>
            <tr><th>时间</th><th>基线</th><th>类型</th><th>作业</th><th>风险</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr v-for="node in board.risk_nodes" :key="node.节点键">
              <td>{{ node.节点时间 || '—' }}</td>
              <td>{{ node.基线编号 }}</td>
              <td>{{ node.节点类型 }}</td>
              <td>{{ node.作业名称 || '—' }}</td>
              <td><span class="pill" :class="riskClass(node.风险级别)">{{ node.风险级别 }}</span></td>
              <td>{{ node.标题 }}</td>
            </tr>
            <tr v-if="!board.risk_nodes.length">
              <td colspan="6" class="empty-state">当前没有破线或预警节点</td>
            </tr>
          </tbody>
        </table>
      </section>
    </template>

    <!-- 概览视图：承诺完成时间排序 + 版本冲突 + 运行结果提交 -->
    <template v-else>
      <div class="stat-row">
        <article v-for="item in overviewCards" :key="item.label" class="stat-card">
          <span class="stat-label">{{ item.label }}</span>
          <strong class="stat-value">{{ item.value }}</strong>
        </article>
      </div>

      <table class="data-table">
        <thead>
          <tr>
            <th>基线编号</th><th>基线名称</th><th>承诺完成时间</th><th>预警时间</th>
            <th>当前版本</th><th>归属版本</th><th>健康状态</th><th>破线风险</th>
            <th>运行水位</th><th>缺失分区</th><th>预计完成时间</th><th>基线结论</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in overviewBaselines" :key="row.基线编号">
            <td>{{ row.基线编号 }}</td>
            <td>{{ row.基线名称 }}</td>
            <td>{{ row.承诺完成时间 || '—' }}</td>
            <td>{{ row.预警时间 || '—' }}</td>
            <td>v{{ row.当前版本 }}</td>
            <td>{{ row.归属版本 || '—' }}</td>
            <td><span class="pill" :class="row.健康状态 === '已破线' ? 'high' : row.健康状态 === '预警中' ? 'mid' : 'low'">{{ row.健康状态 }}</span></td>
            <td>{{ row.破线风险 }}</td>
            <td>{{ row.运行水位 ?? '—' }}%</td>
            <td>{{ row.缺失分区数 ?? 0 }}</td>
            <td>{{ row.预计完成时间 || '—' }}</td>
            <td>{{ row.基线结论 }}</td>
          </tr>
          <tr v-if="!overviewBaselines.length">
            <td colspan="12" class="empty-state">暂无基线数据</td>
          </tr>
        </tbody>
      </table>

      <section class="panel">
        <h4>同一作业被多条基线 / 多个版本引用</h4>
        <p class="hint">引用版本不同会改变预警时间；运行结果未指定版本时按到达时间取最新生效版本。</p>
        <table class="data-table compact">
          <thead>
            <tr><th>作业</th><th>基线</th><th>版本</th><th>承诺完成时间</th><th>预警余量</th><th>预警时间</th><th>生效时间</th></tr>
          </thead>
          <tbody>
            <template v-for="item in conflicts" :key="item.作业名称">
              <tr v-for="(version, index) in item.候选版本" :key="item.作业名称 + version.基线编号 + version.版本号">
                <td>{{ index === 0 ? item.作业名称 : '' }}</td>
                <td>{{ version.基线编号 }}</td>
                <td>v{{ version.版本号 }}</td>
                <td>{{ version.承诺完成时间 || '—' }}</td>
                <td>{{ version.预警余量分钟 }} 分钟</td>
                <td>{{ version.预警时间 || '—' }}</td>
                <td>{{ version.生效时间 || '—' }}</td>
              </tr>
            </template>
            <tr v-if="!conflicts.length">
              <td colspan="7" class="empty-state">没有跨基线或跨版本的作业引用</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="panel">
        <h4>提交作业运行分区结果</h4>
        <p class="hint">同一作业可能属于多条基线；提交后相关时间线与基线状态在同一事务内更新，重复提交只生效一次。</p>
        <form class="filter-bar" @submit.prevent="submitRunResult">
          <label v-for="field in runFields" :key="field" class="filter-item">
            <span>{{ field }}</span>
            <input v-model="runForm[field]" :placeholder="field" />
          </label>
          <button class="btn primary" type="submit">提交运行结果</button>
        </form>
        <p v-if="runMessage" class="hint">{{ runMessage }}</p>
      </section>
    </template>

    <footer class="page-foot">
      <span>{{ footText }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type BaselineState = Record<string, any>
type TimelineNode = Record<string, any>

const ENDPOINT = '/api/baseline'

const tabs = [
  { key: 'timeline', label: '健康时间线' },
  { key: 'board', label: '破线风险看板' },
  { key: 'overview', label: '概览与运行结果' },
]
const runFields = ['运行编号', '作业名称', '数据日期', '期望分区数', '已产出分区数', '缺失分区', '运行水位', '到达时间', '预计完成时间']

const activeTab = ref('timeline')
const overviewCards = ref<{ label: string; value: number }[]>([])
const overviewBaselines = ref<BaselineState[]>([])
const board = ref<{ columns: { key: string; label: string; items: BaselineState[] }[]; risk_nodes: TimelineNode[] }>({
  columns: [],
  risk_nodes: [],
})
const conflicts = ref<{ 作业名称: string; 候选版本: BaselineState[] }[]>([])
const detail = ref<{ baseline: BaselineState; state: BaselineState; versions: BaselineState[]; nodes: TimelineNode[]; attributions: BaselineState[] } | null>(null)
const selectedNo = ref('')
const errorMessage = ref('')
const runMessage = ref('')
const runForm = ref<Record<string, string>>({})
const generatedAt = ref('')

const footText = computed(() =>
  generatedAt.value ? `时间线生成于 ${generatedAt.value}，当前视图：${tabs.find((item) => item.key === activeTab.value)?.label}` : '正在读取基线时间线',
)

function riskClass(value: unknown): string {
  const text = String(value ?? '')
  if (text === '已破线' || text === '高') return 'high'
  if (text === '预警中' || text === '中') return 'mid'
  if (text === '正常' || text === '低') return 'low'
  return 'idle'
}

async function loadJson(path: string): Promise<any> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，数据未更新`)
  }
  return response.json()
}

async function loadOverview() {
  const payload = await loadJson(`${ENDPOINT}/timeline/overview`)
  overviewCards.value = payload.cards ?? []
  overviewBaselines.value = payload.baselines ?? []
  generatedAt.value = payload.generated_at ?? ''
  if (!selectedNo.value && overviewBaselines.value.length) {
    selectedNo.value = String(overviewBaselines.value[0].基线编号)
  }
}

async function loadBoard() {
  const payload = await loadJson(`${ENDPOINT}/timeline/risk-board`)
  board.value = { columns: payload.columns ?? [], risk_nodes: payload.risk_nodes ?? [] }
}

async function loadConflicts() {
  const payload = await loadJson(`${ENDPOINT}/timeline/version-conflicts`)
  conflicts.value = payload.items ?? []
}

async function loadDetail(baselineNo: string) {
  const list = await loadJson(`${ENDPOINT}?size=200`)
  const matched = (list.items ?? []).find((row: BaselineState) => String(row['基线编号']) === baselineNo)
  if (!matched) {
    detail.value = null
    return
  }
  detail.value = await loadJson(`${ENDPOINT}/${matched.id}/timeline`)
}

async function selectBaseline(baselineNo: string) {
  selectedNo.value = baselineNo
  errorMessage.value = ''
  try {
    await loadDetail(baselineNo)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '时间线读取失败'
  }
}

async function switchTab(key: string) {
  activeTab.value = key
  if (key === 'board') {
    await safeLoad(loadBoard)
  } else if (key === 'overview') {
    await safeLoad(loadConflicts)
  } else if (selectedNo.value) {
    await selectBaseline(selectedNo.value)
  }
}

async function safeLoad(loader: () => Promise<void>) {
  errorMessage.value = ''
  try {
    await loader()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据读取失败'
  }
}

async function reloadAll() {
  await safeLoad(async () => {
    await loadOverview()
    await loadBoard()
    await loadConflicts()
    if (selectedNo.value) {
      await loadDetail(selectedNo.value)
    }
  })
}

async function recompute() {
  runMessage.value = ''
  await safeLoad(async () => {
    const response = await request(`${ENDPOINT}/timeline/recompute`, {
      method: 'POST',
      body: JSON.stringify({ values: {} }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message ?? '重算未生效')
    }
    runMessage.value = payload.message
    await loadOverview()
    await loadBoard()
    if (selectedNo.value) {
      await loadDetail(selectedNo.value)
    }
  })
}

async function submitRunResult() {
  runMessage.value = ''
  const values: Record<string, string> = {}
  Object.entries(runForm.value).forEach(([key, value]) => {
    if (String(value ?? '').trim()) {
      values[key] = value
    }
  })
  try {
    const response = await request(`${ENDPOINT}/timeline/run-results`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      throw new Error(payload.message ?? '运行结果提交失败')
    }
    runMessage.value = payload.duplicate
      ? `${payload.message}（交易号 ${payload.transaction}）`
      : `${payload.message}：新增 ${payload.added_nodes} 个节点，涉及 ${(payload.baselines ?? []).map((item: BaselineState) => item['基线编号']).join('、') || '无'}`
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '运行结果提交失败'
  }
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

onMounted(reloadAll)
</script>

<style scoped>
.tab-row { display: flex; gap: 8px; margin-bottom: 12px; }
.tab { border: 1px solid var(--border); background: #fff; border-radius: 6px; padding: 6px 12px; cursor: pointer; font-size: 13px; }
.tab.active { background: var(--brand); border-color: var(--brand); color: #fff; }
.timeline-layout { display: grid; grid-template-columns: 320px 1fr; gap: 16px; align-items: start; }
.baseline-rail { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.rail-item { background: #fff; border: 1px solid var(--border); border-left: 4px solid #94a3b8; border-radius: 8px; padding: 10px 12px; cursor: pointer; display: flex; flex-direction: column; gap: 4px; }
.rail-item.selected { box-shadow: 0 0 0 2px rgba(31, 111, 235, .35); }
.rail-item.high { border-left-color: #b42318; }
.rail-item.mid { border-left-color: #d97706; }
.rail-item.low { border-left-color: #15803d; }
.rail-time { font-size: 12px; color: var(--muted); }
.rail-meta, .rail-desc { font-size: 12px; color: var(--muted); }
.timeline-detail { display: flex; flex-direction: column; gap: 12px; }
.detail-head h3 { margin: 0 0 4px; }
.detail-sub { font-size: 12px; color: var(--muted); }
.detail-conclusion { font-size: 13px; margin: 6px 0 0; }
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.panel h4 { margin: 0 0 8px; font-size: 14px; }
.hint { font-size: 12px; color: var(--muted); margin: 0 0 8px; }
.data-table.compact th, .data-table.compact td { padding: 6px 8px; font-size: 12px; }
.node-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.node-item { display: grid; grid-template-columns: 120px auto 1fr; gap: 8px; align-items: start; border-left: 3px solid #cbd5e1; padding: 6px 8px; background: #fbfcfe; border-radius: 0 6px 6px 0; }
.node-item.high { border-left-color: #b42318; }
.node-item.mid { border-left-color: #d97706; }
.node-time { font-size: 12px; color: var(--muted); }
.node-body strong { font-size: 13px; }
.node-body p { margin: 2px 0; font-size: 12px; }
.node-meta { font-size: 11px; color: var(--muted); }
.pill { display: inline-block; border-radius: 999px; padding: 1px 8px; font-size: 12px; background: #e2e8f0; color: #334155; }
.pill.high { background: #fee4e2; color: #b42318; }
.pill.mid { background: #fef0c7; color: #b54708; }
.pill.low { background: #dcfae6; color: #067647; }
.kanban { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-bottom: 12px; }
.kanban-column { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 8px; display: flex; flex-direction: column; gap: 8px; }
.kanban-column header { display: flex; justify-content: space-between; font-size: 13px; color: var(--muted); }
.kanban-card { display: flex; flex-direction: column; gap: 3px; font-size: 12px; border: 1px solid var(--border); border-left: 4px solid #94a3b8; border-radius: 6px; padding: 8px; }
.kanban-card.high { border-left-color: #b42318; }
.kanban-card.mid { border-left-color: #d97706; }
.kanban-card.low { border-left-color: #15803d; }
.kanban-version { color: var(--muted); }
</style>
