import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { SessionId } from '@deepseek-ai/dsh-session'
import { nativeRuntime } from '../fixtures/native-runtime.js'
import { initialSelections, mountAgents, TextAdapter } from '../fixtures/agent-runtime.js'
import { RuntimeRouter } from '../../src/host/dsh-runtime/router.js'
import { sessionId } from '../../src/host/dsh-runtime/identity.js'

const signal = () => new AbortController().signal

async function until(check: () => boolean) {
  for (let n = 0; n < 500; n++) { if (check()) return; await delay(10) }
  assert.ok(check(), 'expected native progress within 5 seconds')
}

test('queued messages stay in the native inbox and native edit/delete operations are reflected by RPC', { timeout: 30_000 }, async () => {
  const home = await mkdtemp(join(tmpdir(), 'aa-native-queue-'))
  const adapter = new TextAdapter()
  const fixture = await nativeRuntime(home, ctx => mountAgents(ctx, adapter))
  const native = fixture.ctx.agentsAnywhereRuntime.native
  const id = SessionId('queue-integration')
  const platformSessionId = sessionId('queue-test', String(id))
  const router = new RuntimeRouter({ native, query: fixture.ctx.sessionQuery, status: sessionId => native.status(sessionId) }, 'queue-test')
  const params = { sessionId: platformSessionId, externalSessionId: String(id) }
  try {
    await native.send(id, 'active request', 'active-request', home, true, initialSelections, 'standard')
    await until(() => adapter.requests.length === 1 && !!adapter.release)

    await router.request('session.queue', {
      ...params,
      content: 'queued request',
      clientMessageId: 'queued-request',
    }, signal())

    let queue = await router.request('session.queue.list', params, signal()) as {
      source: string
      items: Array<{ id: string; content: string; placement: string }>
    }
    assert.equal(queue.source, 'dsh-native')
    assert.equal(queue.items.length, 1)
    assert.equal(queue.items[0]?.content, 'queued request')
    assert.equal(queue.items[0]?.placement, 'queued')

    const session = fixture.ctx.agents.get(id)!.session
    assert.equal(session.snapshotEvents().some(event => event.type === 'user/message' &&
      event.data.content.some(block => block.type === 'text' && block.text === 'queued request')), false)
    assert.equal(session.snapshotEvents().some(event => event.type === 'agent/inbox/spliced' &&
      event.data.inserted.some(message => message.content.some(block => block.type === 'text' && block.text === 'queued request'))), true)

    await router.request('session.queue.update', {
      ...params,
      itemId: queue.items[0]!.id,
      content: 'edited queued request',
    }, signal())
    queue = await router.request('session.queue.list', params, signal()) as typeof queue
    assert.equal(queue.items[0]?.content, 'edited queued request')

    await router.request('session.queue.delete', { ...params, itemId: queue.items[0]!.id }, signal())
    queue = await router.request('session.queue.list', params, signal()) as typeof queue
    assert.deepEqual(queue.items, [])

    adapter.release!()
    await until(() => fixture.ctx.agents.get(id)?.status === 'idle')
    assert.equal(adapter.requests.length, 1, 'deleted queued messages must never reach the model')
  } finally {
    router.close()
    await fixture.ctx.fiber.dispose()
    await rm(home, { recursive: true, force: true })
  }
})
