import { lazy } from 'react'
import type { FrontendExtension } from '@/extensions/types'
import { IconSignals } from '@/components/BrandIcons'

export const Live = lazy(() => import('./Page').then(m => ({ default: m.Live })))

const extension: FrontendExtension = {
  id: 'official.live-trader',
  apiVersion: 1,
  routes: [
    { id: 'live', path: '/live', component: Live },
  ],
  navigation: [
    { id: 'live', routeId: 'live', label: '实盘交易', icon: IconSignals },
  ],
}

export default extension
