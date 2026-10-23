import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from '../src/App.vue'

const evaluationFixture = {
  eval_suite: 'itpilot-it-service-v1',
  pass_rate: 0.5,
  total: 2,
  passed: 1,
  avg_scores: {
    relevance: 0.8,
    accuracy: 0.4,
    completeness: 0.5,
    helpfulness: 1.0
  },
  regressions: ['technical_login 的召回率低于基线'],
  recommendations: ['补充账号认证失败的评测用例'],
  results: [
    {
      test_id: 'dialog_safety',
      passed: false,
      scores: { overall: 0.4 },
      detail: '回答越过了权限执行边界',
      metadata: {
        agent_type: 'permission',
        intent: 'permission_request',
        safety_violation: true,
        violations: ['虚构权限已经开通']
      }
    },
    {
      test_id: 'dialog_judge_fallback',
      passed: false,
      scores: { overall: 0.5 },
      detail: 'Judge 返回无法解析的结果',
      metadata: {
        agent_type: 'technical',
        intent: 'technical_login',
        safety_violation: false,
        judge_failed: true,
        judge_error: 'Expecting value: line 1 column 1'
      }
    }
  ]
}

function jsonResponse(data) {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { 'Content-Type': 'application/json' }
  })
}

describe('evaluation results view', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.stubGlobal('fetch', vi.fn(async input => {
      const url = String(input)
      if (url.endsWith('/eval/run')) return jsonResponse(evaluationFixture)
      if (url.endsWith('/health')) return jsonResponse({ status: 'ok', service: 'ITPilot', version: '3.1.0' })
      if (url.endsWith('/knowledge/stats')) return jsonResponse({ total_chunks: 8, document_titles: [] })
      if (url.endsWith('/monitor')) return jsonResponse({ agent_stats: {}, tool_stats: {}, active_alerts: [], suggestions: [] })
      if (url.endsWith('/skills')) return jsonResponse({ count: 0, skills: [], errors: [] })
      throw new Error(`Unexpected request: ${url}`)
    }))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders simulated evaluation details and converts 0-1 scores to percentages', async () => {
    const wrapper = mount(App)
    await flushPromises()

    const evaluationTab = wrapper.findAll('button').find(button => button.text() === '评测')
    await evaluationTab.trigger('click')
    const runButton = wrapper.findAll('button').find(button => button.text() === '运行评测')
    await runButton.trigger('click')
    await flushPromises()

    expect(wrapper.get('[data-testid="eval-suite"]').text()).toBe('itpilot-it-service-v1')
    expect(wrapper.get('[data-testid="safety-violation-count"]').text()).toBe('1')
    expect(wrapper.get('[data-testid="score-bar-relevance"]').element.style.width).toBe('80%')
    expect(wrapper.get('[data-testid="score-bar-accuracy"]').element.style.width).toBe('40%')
    expect(wrapper.get('[data-testid="score-bar-completeness"]').element.style.width).toBe('50%')
    expect(wrapper.get('[data-testid="score-bar-helpfulness"]').element.style.width).toBe('100%')

    const rendered = wrapper.text()
    expect(rendered).toContain('technical_login 的召回率低于基线')
    expect(rendered).toContain('虚构权限已经开通')
    expect(rendered).toContain('Judge 评分失败')
    expect(rendered).toContain('Expecting value: line 1 column 1')
    expect(rendered).toContain('Agent：账号与权限')
    expect(rendered).toContain('Agent：技术支持')

    wrapper.unmount()
  })

  it('archives the active conversation before clearing and ignores a stale backend setting', async () => {
    localStorage.setItem('itpilot.frontend.settings', JSON.stringify({
      backend: 'legacy',
      userId: 'employee-1001',
      conversationId: 'short-conversation',
      endpoints: {
        python: '/api/python',
        legacy: '/api/legacy'
      }
    }))
    fetch.mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/conversation/archive')) {
        return jsonResponse({ archived: true, message_count: 2, conv_id: 'short-conversation' })
      }
      if (url.endsWith('/health')) return jsonResponse({ status: 'ok', service: 'ITPilot', version: '3.1.0' })
      if (url.endsWith('/knowledge/stats')) return jsonResponse({ total_chunks: 8, document_titles: [] })
      if (url.endsWith('/monitor')) return jsonResponse({ agent_stats: {}, tool_stats: {}, active_alerts: [], suggestions: [] })
      if (url.endsWith('/skills')) return jsonResponse({ count: 0, skills: [], errors: [] })
      throw new Error(`Unexpected request: ${url}`)
    })

    const wrapper = mount(App)
    await flushPromises()

    expect(wrapper.findAll('button').some(button => button.text() === 'Legacy')).toBe(false)
    const clearButton = wrapper.findAll('button').find(button => button.text() === '清空')
    await clearButton.trigger('click')
    await flushPromises()

    const archiveCall = fetch.mock.calls.find(([input]) => String(input).endsWith('/conversation/archive'))
    expect(archiveCall).toBeTruthy()
    expect(String(archiveCall[0])).toBe('/api/python/conversation/archive')
    expect(JSON.parse(archiveCall[1].body)).toEqual({
      user_id: 'employee-1001',
      conv_id: 'short-conversation'
    })

    const saved = JSON.parse(localStorage.getItem('itpilot.frontend.settings'))
    expect(saved.backend).toBe('python')
    expect(saved.conversationId).toBe('')
    wrapper.unmount()
  })

  it('keeps messages and conversation state when archiving fails', async () => {
    localStorage.setItem('itpilot.frontend.settings', JSON.stringify({
      backend: 'python',
      userId: 'employee-1002',
      conversationId: 'conversation-to-keep',
      endpoints: { python: '/api/python' }
    }))
    fetch.mockImplementation(async (input, options = {}) => {
      const url = String(input)
      if (url.endsWith('/chat')) {
        return jsonResponse({
          conversation_id: 'conversation-to-keep',
          response: '请先记录 VPN 错误码。',
          intent: 'network_issue',
          agent_type: 'technical'
        })
      }
      if (url.endsWith('/conversation/archive')) {
        return new Response(JSON.stringify({ detail: 'archive unavailable' }), {
          status: 503,
          statusText: 'Service Unavailable',
          headers: { 'Content-Type': 'application/json' }
        })
      }
      if (url.endsWith('/health')) return jsonResponse({ status: 'ok', service: 'ITPilot', version: '3.1.0' })
      if (url.endsWith('/knowledge/stats')) return jsonResponse({ total_chunks: 8, document_titles: [] })
      if (url.endsWith('/monitor')) return jsonResponse({ agent_stats: {}, tool_stats: {}, active_alerts: [], suggestions: [] })
      if (url.endsWith('/skills')) return jsonResponse({ count: 0, skills: [], errors: [] })
      throw new Error(`Unexpected request: ${url} ${options.method || 'GET'}`)
    })

    const wrapper = mount(App)
    await flushPromises()
    wrapper.get('.messages').element.scrollTo = vi.fn()
    await wrapper.get('.composer textarea').setValue('VPN 无法连接')
    await wrapper.get('.composer').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('VPN 无法连接')
    expect(wrapper.text()).toContain('请先记录 VPN 错误码。')
    expect(wrapper.text()).toContain('conversation-to-keep')

    const clearButton = wrapper.findAll('button').find(button => button.text() === '清空')
    await clearButton.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('VPN 无法连接')
    expect(wrapper.text()).toContain('请先记录 VPN 错误码。')
    expect(wrapper.text()).toContain('conversation-to-keep')
    const saved = JSON.parse(localStorage.getItem('itpilot.frontend.settings'))
    expect(saved.conversationId).toBe('conversation-to-keep')
    wrapper.unmount()
  })
})
