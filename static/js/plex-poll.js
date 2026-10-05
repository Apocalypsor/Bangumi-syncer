/* Plex 主动同步任务独立于页面请求，刷新页面后仍能查询本轮结果。 */
(() => {
  const syncButton = document.getElementById('plex-poll-sync')
  const fullButton = document.getElementById('plex-poll-full-sync')
  const result = document.getElementById('plex-poll-result')
  if (!syncButton || !fullButton || !result) return
  let timer

  function setRunning(running) {
    syncButton.disabled = running
    fullButton.disabled = running
  }

  async function refresh() {
    clearTimeout(timer)
    try {
      const response = await apiFetch('/api/plex-poll/status')
      const status = response.data
      setRunning(status.running)
      if (status.running) {
        result.textContent = '正在同步 Plex 已看项目，可离开此页。详细结果见同步记录。'
        timer = setTimeout(refresh, 3000)
      } else if (status.last_result) {
        result.textContent = status.last_result.message
      }
    } catch (error) {
      result.textContent = '无法读取同步状态：' + error.message
      setRunning(false)
    }
  }

  async function start(full) {
    setRunning(true)
    try {
      const response = await apiFetch('/api/plex-poll/sync/manual?full=' + full, {
        method: 'POST',
      })
      result.textContent = response.message
      await refresh()
    } catch (error) {
      result.textContent = '无法启动同步：' + error.message
      setRunning(false)
    }
  }

  syncButton.addEventListener('click', () => start(false))
  fullButton.addEventListener('click', () => start(true))
  refresh()
})()
