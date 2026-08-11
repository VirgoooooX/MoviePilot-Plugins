// 兼容 MoviePilot API 包装器和 Axios 原始响应。
export function unwrapResponse(response) {
  if (response && Object.prototype.hasOwnProperty.call(response, 'data') && response.success !== undefined) return response.data
  return response?.data ?? response
}

// 深拷贝配置，避免直接修改宿主传入值。
export function cloneConfig(config) {
  return JSON.parse(JSON.stringify(config || {}))
}
