import { useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'

export function useEvents() {
  const client = useQueryClient()
  useEffect(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const host = import.meta.env.VITE_WS_HOST ?? window.location.host.replace(':5173', ':8000')
    const socket = new WebSocket(`${protocol}://${host}/api/events`)
    socket.onmessage = () => {
      void client.invalidateQueries({ queryKey: ['questions'] })
      void client.invalidateQueries({ queryKey: ['application'] })
      void client.invalidateQueries({ queryKey: ['applications'] })
      void client.invalidateQueries({ queryKey: ['dashboard'] }); void client.invalidateQueries({ queryKey: ['campaigns'] }); void client.invalidateQueries({ queryKey: ['jobs'] }); void client.invalidateQueries({ queryKey: ['direct-application'] })
    }
    return () => socket.close()
  }, [client])
}
