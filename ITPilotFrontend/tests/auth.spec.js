import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  addKnowledge,
  createInitialSettings,
  deleteKnowledgeDocument,
  getKnowledgeDocument,
  listKnowledgeDocuments,
  requestChat,
  requestHealth,
  requestToolTrace,
  saveSettings,
  updateKnowledgeDocument
} from '../src/lib/backends'


function jsonResponse(data) {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { 'Content-Type': 'application/json' }
  })
}


describe('API credentials', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ found: false, trace: {} })))
  })

  it('keeps tokens out of persistent local settings', () => {
    const settings = { ...createInitialSettings(), userToken: 'user-secret', adminToken: 'admin-secret' }
    saveSettings(settings)

    expect(localStorage.getItem('itpilot.frontend.settings')).not.toContain('secret')
    expect(createInitialSettings().userToken).toBe('user-secret')
    expect(createInitialSettings().adminToken).toBe('admin-secret')
  })

  it('uses the correct token for user and admin requests', async () => {
    const settings = { ...createInitialSettings(), userToken: 'user-secret', adminToken: 'admin-secret' }
    await requestHealth('python', settings)
    await requestChat('python', settings, 'VPN 连接失败')
    await requestToolTrace('python', settings, 'request-1')
    await addKnowledge('python', settings, [{ title: 'VPN', content: '步骤' }])

    const calls = fetch.mock.calls
    expect(calls[0][1].headers?.Authorization).toBeUndefined()
    expect(calls[1][1].headers.Authorization).toBe('Bearer user-secret')
    expect(calls[2][1].headers.Authorization).toBe('Bearer user-secret')
    expect(calls[3][1].headers.Authorization).toBe('Bearer admin-secret')
  })

  it('uses the admin token and document id for document management', async () => {
    const settings = { ...createInitialSettings(), userToken: 'user-secret', adminToken: 'admin-secret' }
    await listKnowledgeDocuments('python', settings)
    await getKnowledgeDocument('python', settings, 'doc/1')
    await updateKnowledgeDocument('python', settings, 'doc/1', { title: 'VPN', content: '新版' })
    await deleteKnowledgeDocument('python', settings, 'doc/1')

    const calls = fetch.mock.calls
    expect(calls.map(([url]) => url)).toEqual([
      '/api/python/knowledge/documents',
      '/api/python/knowledge/documents/doc%2F1',
      '/api/python/knowledge/documents/doc%2F1',
      '/api/python/knowledge/documents/doc%2F1'
    ])
    expect(calls.map(([, options]) => options.headers.Authorization)).toEqual(Array(4).fill('Bearer admin-secret'))
    expect(calls[2][1].method).toBe('PUT')
    expect(calls[3][1].method).toBe('DELETE')
  })
})
