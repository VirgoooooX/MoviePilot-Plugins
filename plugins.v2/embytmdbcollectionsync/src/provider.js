// 兼容 MoviePilot API 包装器和 Axios 原始响应，同时保留业务失败消息。
export function unwrapResponse(response) {
  let payload = response
  // Axios 原始响应：业务包装通常位于 response.data。
  if (payload && Object.prototype.hasOwnProperty.call(payload, 'data') && payload.success === undefined) {
    payload = payload.data
  }
  if (payload && payload.success === false) {
    const error = new Error(payload.message || '插件请求失败')
    error.response = payload
    throw error
  }
  if (payload && payload.success === true && Object.prototype.hasOwnProperty.call(payload, 'data')) {
    return payload.data
  }
  return payload?.data ?? payload
}

// 深拷贝配置，避免直接修改宿主传入值。
export function cloneConfig(config) {
  return JSON.parse(JSON.stringify(config || {}))
}
