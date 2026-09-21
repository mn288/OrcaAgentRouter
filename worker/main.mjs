// Out-of-process worker. It never sees the prompt: grading and launch happen in
// `agent-router` inside the terminal the user picked. The worker only checks
// that the classifiers are reachable and keeps a bounded log of agent status
// changes so routing outcomes can be reviewed later.

const DEFAULT_LAYA_URL = 'http://127.0.0.1:8091'
const STATUS_LOG_KEY = 'agent-status-log'
const STATUS_LOG_LIMIT = 200

async function probe(url, timeoutMs = 4000) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const response = await fetch(url, { signal: controller.signal, redirect: 'error' })
    if (!response.ok) return { ok: false, detail: `HTTP ${response.status}` }
    const body = await response.json()
    return { ok: Boolean(body?.ready), detail: body?.ready ? `ready (${body?.loaded?.join(', ') ?? 'model'})` : 'loading' }
  } catch (error) {
    return { ok: false, detail: error?.name === 'AbortError' ? 'timed out' : 'unreachable' }
  } finally {
    clearTimeout(timer)
  }
}

export default async function activate(ctx) {
  const can = (kind) => ctx.grantedCapabilities.includes(kind)

  const notify = async (title, body) => {
    if (can('notifications:show')) {
      await ctx.host.call('notifications.show', { title, body })
    }
    ctx.log(`${title}: ${body}`)
  }

  const readSettings = async () => {
    if (!can('settings:own')) return {}
    const result = await ctx.host.call('settings.get')
    return result?.settings ?? {}
  }

  ctx.commands.register('check-classifiers', async () => {
    const settings = await readSettings()
    const layaUrl = String(settings.layaUrl || DEFAULT_LAYA_URL).replace(/\/$/, '')
    const laya = await probe(`${layaUrl}/health`)
    const jev = settings.jevConfigured === true
      ? 'credentials configured on the routing host'
      : 'not configured (set TYPESAFE_API_KEY where agent-router runs)'
    await notify('Agent Router', `LAYA ${laya.ok ? 'ready' : 'unavailable'} at ${layaUrl} (${laya.detail}). Jev: ${jev}.`)
    return { laya, jev }
  })

  ctx.commands.register('routing-summary', async () => {
    if (!can('storage')) {
      await notify('Agent Router', 'Storage capability not granted; nothing recorded.')
      return { entries: 0 }
    }
    const stored = await ctx.host.call('storage.get', { key: STATUS_LOG_KEY })
    const entries = Array.isArray(stored?.value) ? stored.value : []
    const byState = {}
    for (const entry of entries) byState[entry.state] = (byState[entry.state] ?? 0) + 1
    const summary = Object.entries(byState).map(([state, count]) => `${state}: ${count}`).join(', ') || 'no agent status changes yet'
    await notify('Agent Router', `${entries.length} status changes recorded. ${summary}`)
    return { entries: entries.length, byState }
  })

  if (can('events:subscribe') && can('storage')) {
    ctx.events.on('agent.status.changed', async (payload) => {
      const stored = await ctx.host.call('storage.get', { key: STATUS_LOG_KEY })
      const entries = Array.isArray(stored?.value) ? stored.value : []
      entries.push({
        at: payload.receivedAt,
        worktreeId: payload.worktreeId,
        paneKey: payload.paneKey,
        state: payload.state
      })
      await ctx.host.call('storage.set', {
        key: STATUS_LOG_KEY,
        value: entries.slice(-STATUS_LOG_LIMIT)
      })
    })
    await ctx.host.call('events.subscribe', { events: ['agent.status.changed'] })
  }
}

export async function deactivate() {}
