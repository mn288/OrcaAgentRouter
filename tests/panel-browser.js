// Run against a fresh panel/index.html page with agent-browser eval --stdin.
// Mock only the ORCA host transport; exercise the real DOM and panel handlers.
(async () => {
  const element = id => document.getElementById(id)
  const assert = (condition, message) => { if (!condition) throw new Error(message) }
  const settle = () => new Promise(resolve => setTimeout(resolve, 30))
  const requests = []
  let rejectRead = false
  let accepted = true
  let pong = false
  const host = event => {
    const data = event.data
    if (data.type === 'orca-panel-pong') pong = data.pingId === 42
    if (data.type !== 'orca-panel-action') return
    requests.push(data)
    const context = { displayName: 'test', branch: 'main', terminals: [{ id: 'shell-1' }] }
    const failure = rejectRead && data.action === 'workspace.readContext'
    window.postMessage({ type: 'orca-panel-action-result', requestId: data.requestId,
      ok: !failure, error: failure ? 'workspace unavailable' : undefined,
      result: data.action === 'workspace.readContext' ? context : { accepted } }, '*')
  }
  window.addEventListener('message', host)
  try {
    assert(element('connectJev').disabled, 'Connection must require an explicit terminal')
    element('refresh').click()
    await settle()
    assert(!document.querySelector('input[name=terminal]:checked'), 'Refresh must not select an agent terminal by default')
    document.querySelector('input[name=terminal]').click()
    element('connectJev').click()
    await settle()
    let sent = requests.at(-1)
    assert(sent.action === 'terminal.sendText' && sent.params.text === 'agent-router configure jev', 'Connect command contains only the CLI command')
    assert(sent.params.enter && sent.params.terminalId === 'shell-1', 'Connect targets the selected shell')
    assert(element('backend').value === 'jev', 'Connect selects Jev')
    element('testConnection').click()
    await settle()
    assert(requests.at(-1).params.text === 'agent-router health --backend jev --probe', 'Jev test must perform a real probe')
    element('backend').value = 'laya'
    element('testConnection').click()
    await settle()
    assert(requests.at(-1).params.text.includes('--backend laya'), 'Test follows the selected classifier')
    element('removeJev').click()
    await settle()
    assert(requests.at(-1).params.text === 'agent-router configure jev --remove', 'Remove must not modify shell credentials')
    element('backend').value = 'jev'
    element('prompt').value = 'Explain this function'
    element('prompt').dispatchEvent(new Event('input'))
    element('localOnly').checked = true
    const before = requests.length
    element('send').click()
    await settle()
    assert(requests.length === before, 'Local-only must refuse hosted Jev')
    element('localOnly').checked = false
    element('enter').checked = false
    element('send').click()
    await settle()
    sent = requests.at(-1)
    assert(!sent.params.enter && sent.params.text.startsWith('agent-router route --b64 '), 'Route must respect manual Enter')
    assert(!sent.params.text.includes('--yes'), 'Panel must retain launch approval')
    const payload = JSON.parse(atob(sent.params.text.split(' --b64 ')[1].replace(/-/g, '+').replace(/_/g, '/')))
    assert(payload.prompt === 'Explain this function' && payload.backend === 'jev', 'Route payload preserves the selection')
    accepted = false
    element('testConnection').click()
    await settle()
    assert(element('status').textContent.includes('did not accept'), 'Rejected sends must not claim success')
    rejectRead = true
    element('refresh').click()
    await settle()
    assert(element('connectJev').disabled && element('send').disabled, 'A failed refresh must clear stale terminals')
    window.postMessage({ type: 'orca-panel-ping', pingId: 42 }, '*')
    await settle()
    assert(pong, 'The panel must answer the ORCA liveness watchdog')
    return 'Passed: explicit terminal selection, connect/test/remove, routing approval, refusal paths, stale context, and watchdog.'
  } finally {
    window.removeEventListener('message', host)
  }
})()
