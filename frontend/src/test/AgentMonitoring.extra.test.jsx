import React from 'react'
import {describe, it, expect, vi} from 'vitest'
import {render, screen, waitFor} from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {AgentMonitoring} from '../AdminConsole.jsx'

describe('AgentMonitoring additional tests', () => {
  const taskId = 'extra-task-001'

  function setup(status = 'running', confirmResult = true) {
    const onNotice = vi.fn()
    const request = vi.fn(async (path, options = {}) => {
      if (path === '/api/admin/agent-tasks' && !options.method) {
        return {
          tasks: [{
            id: taskId,
            agent_type: 'daily_report',
            status,
            progress: 10,
            logs: [],
            result: null
          }],
          execution_mode: 'in_process_single_api_worker',
          schedule_enabled: false
        }
      }

      if (path === '/api/admin/agent-tasks/reports') {
        return {reports: []}
      }

      if (path === '/api/admin/agent-tasks' && options.method === 'POST') {
        return {id: 'created-task-001'}
      }

      return {ok: true}
    })

    vi.spyOn(window, 'confirm').mockReturnValue(confirmResult)

    render(
      <AgentMonitoring
        request={request}
        token="test-token"
        onNotice={onNotice}
      />
    )

    return {request, onNotice}
  }

  it('creates a daily report task', async () => {
    const user = userEvent.setup()
    const {request, onNotice} = setup()

    await screen.findByText(taskId)
    await user.click(screen.getByRole('button', {name: 'Run agent'}))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith(
        '/api/admin/agent-tasks',
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify({
            agent_type: 'daily_report',
            params: {}
          })
        })
      )
    })

    expect(onNotice).toHaveBeenCalledWith('Started daily report task')
  })

  it('does not delete when confirmation is rejected', async () => {
    const user = userEvent.setup()
    const {request} = setup('cancelled', false)

    await screen.findByText(taskId)
    await user.click(screen.getByRole('button', {name: 'Delete'}))

    expect(window.confirm).toHaveBeenCalledWith(
      `Delete task ${taskId}?`
    )

    expect(request).not.toHaveBeenCalledWith(
      `/api/admin/agent-tasks/${taskId}`,
      {method: 'DELETE'}
    )

    expect(screen.getByText(taskId)).toBeInTheDocument()
  })
})
