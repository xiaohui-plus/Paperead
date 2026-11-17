import { defineConfig } from 'wxt'

export default defineConfig({
  modules: ['@wxt-dev/module-react'],
  manifest: ({ browser }) => ({
    name: 'Paperead Connector',
    description: '把网页上的论文送进本地 Paperead 文献库',
    version: '0.1.0',
    icons: {
      128: 'icon/128.png',
    },
    action: {
      default_title: 'Paperead Connector',
      default_icon: 'icon/128.png',
    },
    permissions: [
      'storage',
      'activeTab',
      'scripting',
      'contextMenus',
      'notifications',
    ],
    host_permissions: ['http://127.0.0.1/*'],
    optional_host_permissions: ['https://*/*', 'http://*/*'],
    ...(browser === 'firefox'
      ? {
          browser_specific_settings: {
            gecko: { id: 'connector@paperead.app' },
          },
        }
      : {}),
  }),
})
