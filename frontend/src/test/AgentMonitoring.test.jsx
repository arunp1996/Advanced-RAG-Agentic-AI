import React from 'react'
import {describe, it, expect, vi, beforeEach, afterEach} from 'vitest'
import {render, screen, waitFor, cleanup} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {AgentMonitoring} from '../AdminConsole.jsx'

const TASK_ID = 'test-task-001'

const makeTask = (status = 'running') => ({
  id: TASK_ID,
  agent_type: 'daily_report',
  status,
  progress: 25,
  created_at: '2026-10-30T10:00:00Z',
  finished_at: null,
  error_summary: null,
  logs: [],
  result: null
})

describe('AgentMonitoring', () => {
  let request
  let onNotice
  let currentTask

  beforeEach(() => {
    currentTask = makeTask('running')
    onNotice = vi.fn()
    window.confirm = vi.fn(() => true)

    request = vi.fn(async (path, options = {}) => {
      if (path === '/api/admin/agent-tasks' && !options.method) {
        return {
          tasks: currentTask ? [currentTask] : [],
          execution_mode: 'in_process_single_api_worker',
          schedule_enabled: false
        }
      }

      if (path === '/api/admin/agent-tasks/reports') {
        return {reports: []}
      }

      if (path === '/api/admin/agent-tasks/reporting/cycles') {
        return {cycles: [], count: 0}
      }

      if (path === '/api/admin/agent-tasks/reporting/reports') {
        return {reports: [], count: 0}
      }

      if (path === `/api/admin/agent-tasks/${TASK_ID}/pause`) {
        currentTask = {...currentTask, status: 'paused'}
        return {ok: true}
      }

      if (path === `/api/admin/agent-tasks/${TASK_ID}/resume`) {
        currentTask = {...currentTask, status: 'running'}
        return {ok: true}
      }

      if (path === `/api/admin/agent-tasks/${TASK_ID}/cancel`) {
        currentTask = {...currentTask, status: 'cancelled'}
        return {ok: true}
      }

      if (path === `/api/admin/agent-tasks/${TASK_ID}` && options.method === 'DELETE') {
        currentTask = null
        return {ok: true}
      }

      if (path === '/api/admin/agent-tasks' && options.method === 'POST') {
        return {id: 'new-task-001'}
      }

      throw new Error(`Unexpected API request: ${path}`)
    })
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  async function renderMonitoring() {
    const user = userEvent.setup()

    render(
      <AgentMonitoring
        request={request}
        token="test-token"
        onNotice={onNotice}
      />
    )

    await screen.findByText(TASK_ID)
    return user
  }

  it('loads agent tasks and execution information', async () => {
    await renderMonitoring()

    expect(screen.getByText('running')).toBeInTheDocument()
    expect(screen.getByText(/in_process_single_api_worker/)).toBeInTheDocument()
    expect(screen.getByText('25%')).toBeInTheDocument()
  })

  it('pauses a running task after confirmation', async () => {
    const user = await renderMonitoring()

    await user.click(screen.getByRole('button', {name: 'Pause'}))

    expect(window.confirm).toHaveBeenCalledWith(`Pause task ${TASK_ID}?`)
    await waitFor(() => {
      expect(request).toHaveBeenCalledWith(
        `/api/admin/agent-tasks/${TASK_ID}/pause`,
        {method: 'POST'}
      )
    })
    expect(await screen.findByText('paused')).toBeInTheDocument()
  })

  it('resumes a paused task after confirmation', async () => {
    currentTask = makeTask('paused')
    const user = await renderMonitoring()

    await user.click(screen.getByRole('button', {name: 'Resume'}))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith(
        `/api/admin/agent-tasks/${TASK_ID}/resume`,
        {method: 'POST'}
      )
    })
    expect(await screen.findByText('running')).toBeInTheDocument()
  })

  it('cancels a running task after confirmation', async () => {
    const user = await renderMonitoring()

    await user.click(screen.getByRole('button', {name: 'Cancel'}))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith(
        `/api/admin/agent-tasks/${TASK_ID}/cancel`,
        {method: 'POST'}
      )
    })
    expect(await screen.findByText('cancelled')).toBeInTheDocument()
  })

  it('deletes a cancelled task after confirmation', async () => {
    currentTask = makeTask('cancelled')
    const user = await renderMonitoring()

    await user.click(screen.getByRole('button', {name: 'Delete'}))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith(
        `/api/admin/agent-tasks/${TASK_ID}`,
        {method: 'DELETE'}
      )
    })
    expect(await screen.findByText(/No individual agent tasks yet/)).toBeInTheDocument()
  })

  it('does not send an action when confirmation is rejected', async () => {
    window.confirm = vi.fn(() => false)
    const user = await renderMonitoring()

    await user.click(screen.getByRole('button', {name: 'Pause'}))

    expect(window.confirm).toHaveBeenCalled()
    expect(request).not.toHaveBeenCalledWith(
      `/api/admin/agent-tasks/${TASK_ID}/pause`,
      {method: 'POST'}
    )
    expect(screen.getByText('running')).toBeInTheDocument()
  })

  it('displays an API error when an action fails', async () => {
    request.mockImplementation(async (path, options = {}) => {
      if (path === '/api/admin/agent-tasks' && !options.method) {
        return {
          tasks: [makeTask('running')],
          execution_mode: 'in_process_single_api_worker',
          schedule_enabled: false
        }
      }

      if (path === '/api/admin/agent-tasks/reports') {
        return {reports: []}
      }

      if (path === '/api/admin/agent-tasks/reporting/cycles') {
        return {cycles: [], count: 0}
      }

      if (path === '/api/admin/agent-tasks/reporting/reports') {
        return {reports: [], count: 0}
      }

      if (path.endsWith('/pause')) {
        throw new Error('Permission denied')
      }

      throw new Error(`Unexpected API request: ${path}`)
    })

    const user = await renderMonitoring()
    await user.click(screen.getByRole('button', {name: 'Pause'}))

    await waitFor(() => {
      expect(onNotice).toHaveBeenCalledWith('Permission denied')
    })
  })
})
