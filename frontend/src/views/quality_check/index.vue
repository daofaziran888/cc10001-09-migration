<template>
  <section class="page" data-module="quality_check">
    <header class="page-head">
      <div><h2>质量审计</h2><p class="page-desc">维护采集结果的质量审计记录、阈值比对与处理建议，支撑数据质量闭环。</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记审计记录</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出作业清单</button>
      </div>
    </header>
    <div class="filter-bar">
      <label><span>开始日期</span><input type="date" v-model="dateFrom" /></label>
      <label><span>结束日期</span><input type="date" v-model="dateTo" /></label>
      <button class="btn" type="button" @click="reload">查询</button>
    </div>

    <!-- 版本化迁移面板 -->
    <div class="migration-panel" data-testid="migration-panel">
      <div class="migration-head">
        <h3>记录版本化迁移</h3>
        <div class="migration-actions">
          <label class="policy-pick"><span>冲突策略</span>
            <select v-model="conflictPolicy" data-testid="conflict-policy">
              <option value="manual">人工确认（保留冲突清单）</option>
              <option value="archive">新版为准（旧数组归档）</option>
            </select>
          </label>
          <button class="btn primary" type="button" data-testid="run-migration" @click="runMigration">执行存量迁移</button>
          <button class="btn" type="button" data-testid="refresh-versions" @click="loadVersions">刷新审计详情</button>
        </div>
      </div>
      <p v-if="migrationMessage" class="migration-msg" data-testid="migration-message">{{ migrationMessage }}</p>
      <div class="kpi-row" v-if="versionSummary">
        <span class="kpi-chip" data-testid="kpi-migrated">已迁移版本 <strong>{{ versionSummary.migrated_total }}</strong></span>
        <span class="kpi-chip" data-testid="kpi-current">当前版本记录 <strong>{{ versionSummary.current_total }}</strong></span>
        <span class="kpi-chip" data-testid="kpi-pending">待确认冲突 <strong>{{ versionSummary.pending_conflicts }}</strong></span>
        <span class="kpi-chip" data-testid="kpi-archived">归档历史版本 <strong>{{ versionSummary.archived_total }}</strong></span>
        <span class="kpi-chip" data-testid="kpi-unmigrated">未迁移旧记录 <strong>{{ versionSummary.unmigrated_total }}</strong></span>
      </div>

      <div v-if="conflicts.length" class="conflict-block" data-testid="conflict-block">
        <h4>冲突清单（等待人工确认，不会静默覆盖历史）</h4>
        <table class="data-table">
          <thead><tr><th>记录编号</th><th>新版本</th><th>新结论</th><th>当前结论</th><th>来源</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="c in conflicts" :key="c.envelope_id" :data-testid="`conflict-row-${c.envelope_id}`">
              <td>{{ c.record_no }}</td>
              <td>v{{ c.version }}</td>
              <td>{{ c.verdict }}</td>
              <td>{{ c.conflict?.current_verdict }}</td>
              <td>{{ c.source?.type }} / {{ c.source?.file }}</td>
              <td>
                <button class="btn primary" type="button" @click="resolveConflict(c.envelope_id, 'use_incoming')">采用新版</button>
                <button class="btn" type="button" @click="resolveConflict(c.envelope_id, 'keep_current')">维持当前</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <details class="version-details" data-testid="version-details">
        <summary>版本化数据集（{{ migratedEnvelopes.length }} 条信封，含归档与待确认）</summary>
        <table class="data-table">
          <thead><tr><th>记录编号</th><th>版本</th><th>状态</th><th>关联批次</th><th>检查项</th><th>判定结果</th><th>来源</th><th>来源版本</th></tr></thead>
          <tbody>
            <tr v-for="env in migratedEnvelopes" :key="env.id" :data-testid="`env-row-${env.id}`" :data-status="env.lifecycle">
              <td>{{ env.record_no }}</td>
              <td>v{{ env.version }}{{ env.is_current ? ' ★' : '' }}</td>
              <td>{{ lifecycleLabel(env.lifecycle) }}</td>
              <td>{{ env.batch_no }}</td>
              <td>{{ env.check_item }}</td>
              <td>{{ env.verdict }}</td>
              <td>{{ env.source?.type }} / {{ env.source?.file }}</td>
              <td>{{ env.source?.source_version ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
      </details>

      <details v-if="unmigratedRows.length" class="version-details" data-testid="unmigrated-details">
        <summary>未迁移旧数组记录（{{ unmigratedRows.length }} 条，读取服务仍可看到）</summary>
        <table class="data-table">
          <thead><tr><th>记录编号</th><th>关联批次</th><th>检查项</th><th>判定结果</th><th>来源文件</th></tr></thead>
          <tbody>
            <tr v-for="row in unmigratedRows" :key="`${row.source.file}-${row.source.record_id}`">
              <td>{{ row.record_no }}</td><td>{{ row.batch_no }}</td><td>{{ row.check_item }}</td>
              <td>{{ row.verdict }}</td><td>{{ row.source?.file }}</td>
            </tr>
          </tbody>
        </table>
      </details>
    </div>

    <div class="timeline-wrap">
      <div v-for="item in timelineItems" :key="item.id" class="timeline-row">
        <span class="timeline-label">{{ item.name || item.label }}</span>
        <div class="timeline-track">
          <div class="timeline-bar" :class="`status-${item.status}`" :style="{ left: barLeft(item) + '%', width: barWidth(item) + '%' }" :data-testid="`bar-${item.id}`" :title="`${item.start} ~ ${item.end}`" @click="selectItem(item)">&nbsp;</div>
        </div>
        <span style="font-size:12px;color:var(--muted);margin-left:8px;">{{ item.start }} ~ {{ item.end }}</span>
      </div>
      <p v-if="!timelineItems.length" style="text-align:center;color:var(--muted)">暂无作业记录</p>
    </div>
    <div v-if="selectedItem" class="timeline-log" data-testid="timeline-log">
      <strong>{{ selectedItem.name || selectedItem.label }}</strong> — 状态：{{ selectedItem.status }}
      <div style="margin-top:4px;">{{ selectedItem.logs || '暂无日志' }}</div>
    </div>
    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记审计记录</h3>
        <label v-for="field in columns.slice(0,3)" :key="field" class="create-field"><span>{{ field }}</span><input v-model="createValues[field]" :placeholder="`请输入${field}`" required /></label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showCreate=false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>
  </section>
</template>
<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/quality_check'
const columns = ["记录编号", "关联批次", "检查项", "阈值", "实际值", "判定结果", "审计时间", "处理建议"]
const actions = ["执行审计", "确认通过", "提交修复"]
const timelineItems = ref<any[]>([])
const selectedItem = ref<any>(null)
const dateFrom = ref(''); const dateTo = ref('')
const errorMessage = ref(''); const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
const allItems = ref<any[]>([])

// 版本化迁移状态
const conflictPolicy = ref<'manual' | 'archive'>('manual')
const versionSummary = ref<any>(null)
const migratedEnvelopes = ref<any[]>([])
const unmigratedRows = ref<any[]>([])
const conflicts = ref<any[]>([])
const migrationMessage = ref('')

function lifecycleLabel(lifecycle: string) {
  return ({ current: '当前版本', archived: '已归档', pending_confirm: '待确认', unmigrated: '未迁移' } as Record<string, string>)[lifecycle] || lifecycle
}
function barLeft(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 0; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return ((new Date(item.start).getTime() - min) / range) * 100 }
function barWidth(item: any) { const all = allItems.value.filter(i => i.start && i.end); if(!all.length) return 20; const min = Math.min(...all.map(i => new Date(i.start).getTime())); const max = Math.max(...all.map(i => new Date(i.end).getTime())); const range = max - min || 86400000; return Math.max(5, ((new Date(item.end).getTime() - new Date(item.start).getTime()) / range) * 100) }
function selectItem(item: any) { selectedItem.value = selectedItem.value?.id === item.id ? null : item }
async function reload() {
  try { const res = await request(ENDPOINT); const data = await res.json(); allItems.value = data.items || []; applyFilter() } catch(e) { errorMessage.value = '加载失败' }
}
function applyFilter() {
  let items = allItems.value
  if(dateFrom.value) items = items.filter((i: any) => i.start >= dateFrom.value)
  if(dateTo.value) items = items.filter((i: any) => i.end <= dateTo.value)
  timelineItems.value = items; selectedItem.value = null
}
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try { const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }); if(!res.ok) throw new Error('创建失败'); showCreate.value = false; await reload() } catch(e: any) { errorMessage.value = e.message }
}
function exportRows() { window.open(`${ENDPOINT}/export`, '_blank') }

async function loadVersions() {
  try {
    const res = await request(`${ENDPOINT}/versions`)
    const data = await res.json()
    versionSummary.value = data.summary
    migratedEnvelopes.value = data.migrated || []
    unmigratedRows.value = data.unmigrated || []
    const conflictRes = await request(`${ENDPOINT}/conflicts`)
    conflicts.value = (await conflictRes.json()).items || []
  } catch(e: any) { migrationMessage.value = `刷新失败：${e.message}` }
}
async function runMigration() {
  migrationMessage.value = '迁移中…'
  try {
    const res = await request(`${ENDPOINT}/migration/run`, {
      method: 'POST',
      body: JSON.stringify({ conflict_policy: conflictPolicy.value }),
    })
    if (!res.ok) throw new Error(`迁移失败（${res.status}）`)
    const data = await res.json()
    const b = data.batch
    migrationMessage.value = `整组提交完成：扫描 ${b.scanned} 条，新迁移 ${b.migrated} 条，幂等跳过 ${b.skipped} 条，冲突 ${b.conflicts} 条，数据集共 ${data.dataset_size} 条`
    await loadVersions()
  } catch(e: any) { migrationMessage.value = e.message }
}
async function resolveConflict(envelopeId: number, resolution: string) {
  try {
    const res = await request(`${ENDPOINT}/conflicts/${envelopeId}/resolve`, {
      method: 'POST',
      body: JSON.stringify({ resolution }),
    })
    if (!res.ok) throw new Error('确认失败')
    migrationMessage.value = resolution === 'use_incoming' ? '已采用新版结论，旧版本归档保留' : '已维持当前结论，新版本归档保留'
    await loadVersions()
  } catch(e: any) { migrationMessage.value = e.message }
}
onMounted(() => { reload(); loadVersions() })
</script>
<style scoped>
.migration-panel { border: 1px solid var(--border, #d9d9d9); border-radius: 8px; padding: 14px 16px; margin: 12px 0 18px; background: var(--panel-bg, #fafafa); }
.migration-head { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
.migration-head h3 { margin: 0; font-size: 16px; }
.migration-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.policy-pick { display: flex; align-items: center; gap: 6px; font-size: 13px; }
.migration-msg { margin: 8px 0 0; color: var(--primary, #1677ff); font-size: 13px; }
.kpi-row { display: flex; gap: 10px; flex-wrap: wrap; margin: 12px 0; }
.kpi-chip { background: #fff; border: 1px solid var(--border, #d9d9d9); border-radius: 6px; padding: 6px 12px; font-size: 13px; }
.kpi-chip strong { font-size: 16px; margin-left: 4px; }
.conflict-block { margin: 10px 0; }
.conflict-block h4 { margin: 6px 0; color: #cf1322; font-size: 14px; }
.version-details { margin-top: 10px; }
.version-details summary { cursor: pointer; font-size: 13px; color: var(--muted); margin-bottom: 6px; }
</style>
