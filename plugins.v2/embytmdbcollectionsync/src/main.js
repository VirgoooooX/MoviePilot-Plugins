import { createApp } from 'vue'
import AppPage from './components/AppPage.vue'

// 本地构建入口；运行时由 MoviePilot 联邦加载暴露组件。
createApp(AppPage).mount('#app')
