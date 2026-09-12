import { useEffect, useState, useCallback } from 'react'
import { api, eventStream } from './api'
import { mergeEvents, TERMINAL } from './domain'

const eventTypes = ['run.started', 'run.completed', 'run.failed', 'stage.started', 'stage.completed', 'tool.started', 'tool.completed', 'tool.failed', 'tool.cached', 'conversation.message', 'review.required', 'artifact.created', 'revision.created']

export default function useResearch(runId) {
  const [snapshot, setSnapshot] = useState({ record: null, messages: [], events: [], revisions: [], artifacts: [] })
  const [failure, setFailure] = useState({ runId: null, message: '' })
  const [refreshToken, setRefreshToken] = useState(0)
  const refresh = useCallback(() => setRefreshToken(v => v + 1), [])

  useEffect(() => {
    const controller = new AbortController()
    const options = { signal: controller.signal }
    let disposed = false, busy = false, stream, timer, cursor = 0, streamOpened = false
    if (!runId) return () => controller.abort()
    const pull = async () => {
      if (disposed || busy) return
      busy = true
      try {
        const [record, revisions, incoming] = await Promise.all([
          api(`/api/runs/${runId}`, options), api(`/api/runs/${runId}/revisions`, options), api(`/api/runs/${runId}/events/history?after=${cursor}`, options),
        ])
        const chain = [record]
        while (chain[0].parent_run_id && chain.length < 12) {
          const parent = revisions.find(r => r.run_id === chain[0].parent_run_id)
          if (!parent) break
          chain.unshift(parent)
        }
        const [messageGroups, artifacts] = await Promise.all([
          Promise.all(chain.map(r => api(`/api/runs/${r.run_id}/messages`, options).then(messages => messages.map(m => ({ ...m, revision: r.revision }))))),
          api(`/api/runs/${runId}/artifacts`, options),
        ])
        if (disposed) return
        if (incoming.length) cursor = Math.max(cursor, incoming.at(-1).sequence)
        const messages = messageGroups.flat().sort((a, b) => a.created_at.localeCompare(b.created_at))
        setSnapshot(previous => ({ record, revisions, messages, artifacts, events: mergeEvents(previous.record?.run_id === runId ? previous.events : [], incoming) }))
        setFailure({ runId, message: '' })
        if (TERMINAL.has(record.status)) {
          stream?.close(); clearInterval(timer)
        } else if (!streamOpened) {
          streamOpened = true
          stream = eventStream(runId, cursor)
          for (const type of eventTypes) stream.addEventListener(type, event => {
            if (disposed) return
            try {
              const item = JSON.parse(event.data)
              setSnapshot(previous => ({ ...previous, events: mergeEvents(previous.events, [item]) }))
              // The polling cursor advances only after a complete persisted read.
              if (['run.completed', 'run.failed', 'review.required'].includes(type)) void pull()
            } catch { /* Polling recovers malformed or interrupted event delivery. */ }
          })
        }
      } catch (err) {
        if (!disposed && err.name !== 'AbortError') setFailure({ runId, message: err.message })
      } finally { busy = false }
    }
    timer = setInterval(pull, 1400)
    void pull()
    return () => { disposed = true; controller.abort(); stream?.close(); clearInterval(timer) }
  }, [runId, refreshToken])

  const current = snapshot.record?.run_id === runId ? snapshot : { record: null, messages: [], events: [], revisions: [], artifacts: [] }
  return { ...current, error: failure.runId === runId ? failure.message : '', loading: Boolean(runId && !current.record), refresh }
}
