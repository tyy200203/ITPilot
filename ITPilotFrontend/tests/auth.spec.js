import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  addKnowledge,
  createInitialSettings,
  requestChat,
  requestHealth,
  requestToolTrace,
  saveSettings
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
})
